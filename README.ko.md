# grokbot-activity-log

**Action Recording이 없는 플랜에서도 [Grok Bot](https://docs.x.ai/grok-bot/overview) 활동을 완전하고 위변조 탐지가 가능한 형태로 기록합니다.**

[English README](README.md) · MIT 라이선스 · Python 3.8+ 표준 라이브러리만 사용 · 단일 파일

Grok Bot의 공식 로그(Action Recording, OpenTelemetry Export, Audit Logs)는
[Enterprise 전용](https://docs.x.ai/grok-bot/security)입니다. SuperGrok / Cursor 개인 플랜에서
봇이 무엇을 했는지 남는 기록은 앱 안의 트랜스크립트뿐이고, 내보내기도 안 됩니다.
이 프로젝트는 봇이 작업하면서 반드시 써야 하는 원장(ledger)을 제공하고, 그 원장을
위변조하기 어렵게 만들고, 매일 내 소유 레포로 푸시합니다.

## 구성

| 구성요소 | 위치 | 역할 |
|---|---|---|
| `gblog.py` | 레포 루트 | CLI: `task-start`, `log`, `approval`, `note`, `task-end`, `verify`, `report`, `status`, `tail`, `sync`, `export`, **`capture`, `coverage`, `tune`, `archive`, `alias`** |
| 스킬: activity-log | `skills/activity-log/SKILL.md` | 봇이 언제·어떻게 기록할지 정확히 지시. 프라이빗 스킬로 저장 |
| 스킬: bot-tune | `skills/bot-tune/SKILL.md` | 원장을 근거로 봇 description·스킬·루틴·Auto-review 규칙·메모리 수정안을 만들고, 승인 후 적용·기록·효과 측정하는 주간 루프 |
| 설명 규칙 | `docs/bot-description.md` | 각 봇 description에 붙여 세션이 바뀌어도 기록 규칙 유지 (한/영) |
| 일일 점검 루틴 | `docs/routine-daily-audit.md` | verify → report → commit/push → 요약 게시 (한/영 프롬프트) |
| 주간 튠 루틴 | `docs/routine-weekly-tune.md` | capture → coverage → tune → 질문 → 적용 → 기록 (한/영 프롬프트) |
| 마켓플레이스 킷 | `docs/marketplace.md` | 공유용 "Ledger" 봇의 리스팅 문구·운영 지침·스킬/루틴/메모리 구성 |
| 봇 템플릿 | `docs/bot-template.md` | x.ai/bot에 공유할 수 있는 "Ledger" 봇 정의와 기존 노트형 봇과의 비교 |
| 테스트 + CI | `tests/`, `.github/workflows/ci.yml` | 위변조 탐지·커버리지 매칭·튠 루프 포함 20개 단위 테스트, Python 3.8/3.10/3.12, shellcheck |

## 동작 원리

- **봇별 해시 체인.** 기록은 `logs/<bot>/YYYY-MM-DD.jsonl`에 쌓이며 각 레코드는 `seq`, `prev`(이전 레코드 해시),
  `hash`(정규화 JSON의 SHA-256)를 가집니다. `gblog verify`가 체인을 따라가며 수정·삭제·중복·절단된 첫 줄을 보고합니다.
  체인 헤드는 `state/<bot>.json`에도 저장되어 로그 꼬리를 몰래 지워도 탐지됩니다.
- **병렬 봇 안전.** 모든 봇이 한 컴퓨터를 공유하므로 봇별 `flock`으로 동시 쓰기가 섞이지 않게 합니다.
- **디스크에 닿기 전에 시크릿 제거**: PEM 블록, GitHub/Slack/AWS/OpenAI/xAI/Google 키 형태, JWT, `Bearer …`,
  `password=…`, `?token=…` 쿼리 파라미터, `user:pass@` URL. (패턴 매칭이 전부를 잡지는 못하므로 봇에게도 자격증명을 붙이지 말라고 지시합니다.)
- **사람이 읽는 출력.** `gblog report`는 봇·작업별로 묶인 Markdown 일일 리포트(FAIL/DENIED 표시), `gblog status`는 체인 헤드·열린 작업·동기화 상태.
- **내가 통제하는, 기본 비공개 백업.** `gblog sync`가 `logs/`, `reports/`, `state/`, `tuning/`, `archive/`를 원장 루트의 git 레포에 커밋하고 설정한 리모트로 푸시합니다. 리모트를 설정하지 않으면 아무것도 밖으로 나가지 않고, **원격이 공개 GitHub 레포면 푸시를 거부**합니다(`gh` 또는 공개 API로 확인; `--allow-public`으로만 허용, 확인 불가한 호스트는 경고만). 동기화 자체도 기록됩니다(`--bot` 미지정 시 `system` 봇 체인).
- **의존성 0.** Grok Bot 문서는 수동 설치 패키지는 사라질 수 있다고 경고합니다. `gblog`는 표준 라이브러리 단일 스크립트이고 `install.sh`로 몇 초 안에 재설치됩니다.

## 봇이 잊어도 남는 증거: `capture` 와 `coverage`

자기 보고는 봇의 성실성에 달려 있으므로 v0.2는 봇의 협조 없이도 증거를 모읍니다.

```bash
gblog capture shell-hook --bot ops --install   # bash PROMPT_COMMAND 훅: 모든 명령 → 원장(source=auto)
gblog capture browser --bot ops                 # 컴퓨터 Chrome History의 새 방문(host/path만)
gblog capture files --bot ops --path /workspace # 마지막 스냅샷 이후 생성/수정/삭제된 파일
gblog coverage                                  # 관측된 행동 중 봇이 스스로 보고한 비율(봇·종류별)
```

자동 레코드는 `source: "auto"`를 가지며 봇·종류·정규화된 대상(host / 명령 이름 / 파일 이름)·±30분 창으로 자기 보고와 매칭됩니다.
낮은 커버리지는 측정 가능한 기록 누락이고, 튠의 입력이 됩니다.

## 봇을 원장으로 튠하기: `tune`

개인 플랜에서 모델 가중치는 못 바꾸지만 봇의 나머지 전부(description·스킬·루틴·Auto-review 규칙·메모리)는 바꿀 수 있습니다.
`gblog tune`은 최근 28일을 읽어 `tuning/<날짜>.md`에 근거가 붙은 제안을 씁니다.

| 원장에서 보이는 신호 | 제안 |
|---|---|
| 같은 행동/대상이 2회 이상 실패 | `skill` — 단계 또는 대안 경로 추가 |
| 같은 승인이 2회 이상 거부 / 거부 없이 3회 이상 허용 | `approval` — Always deny + description 규칙 / Allow automatically 규칙 |
| 같은 작업이 3일 이상 수동 시작 | `routine` — 전형적인 시작 시각 포함 |
| 같은 3단계 행동 시퀀스가 3개 이상 작업에서 반복 | `skill` — 스킬로 문서화 |
| 중앙값 2배 넘게 걸린 작업 | `description` — 체크포인트, 일찍 질문 |
| 교훈이 담긴 note("항상/절대/다음부터…") | `memory` |
| 종류별 커버리지 80% 미만, 닫히지 않은 작업 | `logging` |
| 적용 7일 후 지표가 나빠진 변경 | `revert` |

제안마다 id·근거 레코드·지표와 기준값이 붙습니다. 봇이 정확한 수정안을 작성 → 사용자가 승인 → `gblog tune --apply <id> --detail "..."`(또는 `--reject`)로 기록.
다음 주 리포트가 적용 전/후 지표를 비교해(`too early` / `improving` / `not helping`) 루프를 닫습니다. `bot-tune` 스킬과 주간 루틴이 이 과정을 끝까지 수행합니다.

## 주장이 아니라 증거 파일: `archive`

"트랜스크립트를 읽었다"는 *주장*만 저장하는 장부는 약합니다. `gblog archive`는 대화 원문·메시지 본문·결과물 파일 자체를
`archive/<bot>/<날짜>/<sha256>.gz`에 콘텐츠 주소 방식으로 저장하고 해시를 체인에 기록합니다.

```bash
gblog archive --bot ledger --kind transcript --file transcripts/kim-biseo/2026-10-16.jsonl --task-id "$TID"
cat body.txt | gblog archive --bot ops --kind message --stdin --name "거래처 회신"
```

텍스트는 해시 전에 마스킹되고(원장이 시크릿이 남은 파일을 가리키지 않도록; 원문 그대로 저장하려면 `--no-redact`),
같은 내용은 한 번만 저장되며, `gblog verify`가 모든 보관 파일을 다시 해시해 수정·삭제를 로그 줄 편집과 똑같이 FAIL로 잡습니다.
개인 플랜 Grok Bot은 같은 계정 다른 봇의 대화 원문을 읽을 수 있으므로, 일일 루틴이 봇별 트랜스크립트를 보관하면 원장이 일기가 아닌 기록이 됩니다.

## 봇 하나, 이름 하나: `alias`

봇 이름은 `kim`, `kim-primary`, `KimBisunim`처럼 갈라지고, 한글 표시 이름은 체인 키로 쓸 수 없습니다. `alias`는 기존 체인을 하나도 다시 쓰지 않고 둘을 해결합니다.

```bash
gblog alias add --slug kim-biseo --display 김비서님 --alias kim --alias kim-primary --alias KimBisunim
gblog alias list            # slug·표시 이름·변형·디스크의 체인·미등록 체인
gblog alias strict on       # 모르는 이름을 경고 대신 거부
```

- 정식 키는 단일 ASCII slug. `report`·`status`·`tail`·`coverage`·`tune`이 모든 변형 체인을 합산하고 `김비서님(kim-biseo)` 형식으로 표시합니다.
- 변형 이름이나 표시 이름으로 쓴 새 레코드는 slug 체인에 들어가고, 옛 체인은 그대로 유지되며 verify도 그대로 통과합니다.
- 모르는 이름은 기록은 되지만 stderr에 경고(`strict on`이면 거부).
- 같은 제목의 작업이 같은 봇의 다른 체인에서 닫힌 경우 리포트에 표시: `outcome **open** — likely closed as `a2` in chain `kim-primary` (ok)`.

## Grok Bot 컴퓨터에 설치 (5분)

1. 봇에게:
   > https://github.com/deximple/grokbot-activity-log 를 /workspace에 clone하고 `install.sh`를 실행해. 그 다음 `gblog status`를 실행해.
2. 스킬 저장: `skills/activity-log/SKILL.md` 내용을 붙이고 *"이걸 activity-log라는 프라이빗 스킬로 저장해"*.
3. 기록할 모든 봇의 description에 `docs/bot-description.md`의 한국어 스니펫을 추가 (`<BOT_NAME>`은 짧은 이름으로).
4. (권장) 백업용 **프라이빗** 레포를 만들고 봇에게:
   > `gblog sync --remote <git-url> --no-push`를 한 번 실행하고, 필요하면 컴퓨터 브라우저에서 GitHub 로그인 / `gh auth login`을 요청해.
   토큰은 해당 레포 하나로 범위를 제한한 fine-grained 토큰을 쓰고, 채팅이 아닌 보안 시크릿 요청으로 입력합니다.
5. `docs/routine-daily-audit.md`의 일일 점검 루틴과 `docs/routine-weekly-tune.md`의 주간 튠 루틴을 만듭니다. `skills/bot-tune/SKILL.md`도 스킬로 저장합니다.
6. 루틴이 승인에서 멈추지 않도록 Auto-review에 `/workspace/activity-log`에서의 `gblog` 명령 *Allow automatically* 규칙을 추가합니다.

## 한계 — 꼭 읽어주세요

- **원장은 봇의 자기 보고입니다.** `gblog`는 "기록된 뒤 바뀌지 않았음"을 증명하지만, "봇이 모든 것을 기록했음"은 증명하지 못합니다.
  내장된 완화책: 작업마다 필수 `task-start`/`task-end` 쌍, 모든 답변의 Ledger 푸터, `docs/routine-daily-audit.md`의 주간 트랜스크립트 대조,
  누락 발견 시 (백데이팅이 아닌) `note` 추가 규칙.
- Auto Review는 메모리 쓰기를 검토하지 않고, 루틴의 승인은 약 10분 후 만료되므로 `gblog sync`를 사전 승인하지 않으면 로컬 커밋만 됩니다.
- **Enterprise**라면 Action Recording + OpenTelemetry Export + conversation content export를 켜는 것이 정답(사실 기록)이고, `gblog`는 "왜 그랬는지"(note, 결정)를 보완하는 용도로 쓰세요.

## Grok Bot 컴퓨터에서 업그레이드

```bash
git -C /workspace/grokbot-activity-log pull && bash /workspace/grokbot-activity-log/install.sh && gblog --version
gblog verify --all                  # 로그 포맷 호환, 마이그레이션 없음
gblog alias add --slug <slug> --display <표시이름> --alias <옛이름> ...   # 이름 변형이 있는 봇마다 한 번
```

## 라이선스

MIT — [LICENSE](LICENSE).
