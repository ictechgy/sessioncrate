# SessionCrate

**AI 코딩 세션을 압축 보관하고, 검색하고, 복원합니다.**

SessionCrate는 오래된 코딩 도구 기록으로 늘어나는 디스크 사용량을 줄이면서 필요한 내용을 다시 찾을 수 있게 하는 로컬 CLI입니다. 검증된 압축 아카이브를 만들고, 평문 검색 인덱스를 별도로 만들지 않은 채 대화와 도구 출력을 검색하며, 필요할 때 원본 바이트를 복원합니다.

Python 기본 라이브러리만 사용하고 모델 호출·네트워크 전송·텔레메트리를 하지 않습니다. 현재는 초기 알파 버전입니다. 합성 기록으로 보관·복원 무결성을 검증했지만, 각 AI 도구의 목록·사이드바·native resume 동작은 인증하지 않았습니다. 원본을 제거하기 전에 [지원 범위와 검증 근거](docs/compatibility.md)를 확인하세요.

[English README](README.md) · [CI](https://github.com/ictechgy/sessioncrate/actions/workflows/tests.yml) · [기여 안내](CONTRIBUTING.md) · [보안 안내](SECURITY.md)

## 빠른 시작

저장소를 내려받아 전용 가상환경에 설치한 뒤 지원 도구를 확인합니다.

```bash
git clone https://github.com/ictechgy/sessioncrate.git
cd sessioncrate
python3 -m venv .venv
.venv/bin/python -m pip install .
. .venv/bin/activate
sessioncrate tools
```

설치 전 체크아웃에서 바로 실행하려면 `python3 bin/sessioncrate`를 사용할 수 있습니다. Python 3.10 이상이 필요합니다. 원본 제거에는 `lsof`가 추가로 필요하지만, 압축·검색·검증·복원에는 필요하지 않습니다. 별도로 설치할 Python 런타임 패키지는 없으며 PyPI에는 아직 게시하지 않았습니다.

개인 기록을 선택하기 전에 삭제해도 되는 임시 데이터로 먼저 실행하세요.

```bash
python Scripts/demo.py
python Scripts/roundtrip_check.py --seed 12345  # 선택 사항: lsof가 필요한 긴 검사
```

데모는 압축, 원본이 없는 상태에서의 검색, 정확한 복원을 보여줍니다. 왕복 검사는 지원 어댑터 형식별 임시 세션을 만들고 실제 CLI·`lsof`를 사용해 원본 정리, 다른 위치로의 복원, 해시·권한·수정 시각·빈 디렉터리·반복 작업을 검사합니다. 개인 기록은 읽지 않습니다.

## 보관 후 공간 회수

먼저 후보를 미리 봅니다. `archive`는 `--execute`를 지정하기 전까지 미리보기이며, 실행해도 기본값은 원본 유지입니다.

```bash
sessioncrate scan --tool codex --older-than 30
sessioncrate archive --tool codex --older-than 30
sessioncrate archive --tool codex --older-than 30 --execute
sessioncrate stats --json
sessioncrate stats --tool claude --json  # Claude 아카이브와 남아 있는 원본만 집계
```

원본을 유지하면 압축본만큼 저장 공간이 추가됩니다. 공간 회수는 검증된 원본을 제거한 뒤 발생하며, `stats`는 압축본·메타데이터·남아 있는 원본의 용량을 함께 계산합니다. 정리할 대상을 다시 미리 보고 명시적으로 실행하세요.

```bash
sessioncrate prune --tool codex
sessioncrate prune --tool codex --execute
```

`archive --execute --remove-originals`로 보관과 원본 정리를 함께 실행할 수도 있습니다. 이 경우에도 변경되지 않았고 검증된 원본만 확인한 뒤 제거합니다. 최근 파일, 내용이 바뀐 파일, 사용 중인 파일, 안전성을 확인할 수 없는 파일은 보존합니다. 다른 세션은 계속 실행해도 되지만, 정리 대상 세션 자체는 작업 중 다시 열지 마세요. `lsof`는 열린 파일 핸들을 확인할 뿐 애플리케이션의 논리적 세션 상태를 확인하지 않습니다. native resume의 한계는 [지원 범위와 검증 근거](docs/compatibility.md)를 참고하세요.

묶음 세션의 경과 시간은 빈 동반 폴더를 포함한 모든 선택 파일·폴더의 수정 시각으로 판단합니다. Claude 등 묶음 어댑터의 `stats --tool TOOL`은 선택한 메타데이터, 해당 메타데이터가 참조하는 중복 없는 압축본, 남아 있는 원본을 집계합니다. 필터 없는 `stats`는 참조가 없는 압축본까지 포함합니다. 여러 도구 라벨이 공유하는 압축본은 각 도구별 조회에 한 번씩, 전체 조회에는 한 번만 집계됩니다.

## 지원하는 도구

```bash
sessioncrate tools
```

| `--tool` | 입력 방식 | 보관 범위 |
| --- | --- | --- |
| `codex` | Codex CLI 로컬 rollout JSONL | `sessions/`와 `archived_sessions/`의 rollout 파일 |
| `claude` | Claude Code CLI 로컬 기록 | 프로젝트 main JSONL과 같은 세션의 companion 파일, subagent·tool result 포함 |
| `grok` | Grok Build 로컬 세션 폴더 | `sessions/<project>/<session>/` 전체 묶음. ACP 해석은 best effort |
| `gemini` | Gemini CLI 로컬 chat JSON | `tmp/<project>/chats/session-*.json` |
| `agy` | 명시적 텍스트 export | JSON, JSONL, Markdown, 텍스트. Antigravity 내부 SQLite는 지원하지 않음 |
| `glm` | 명시적 텍스트 export | 사용자가 지정한 출처 라벨이며 native 저장 형식이 아님 |
| `opencode` | 명시적 export | OpenCode JSON·텍스트 export. 공유 native DB는 건드리지 않음 |
| `export` | 명시적 텍스트 export | 사용자가 저장한 그 밖의 대화 export |

Native 어댑터는 알려진 기본 경로를 사용하며 `--source-root`로 지정할 수도 있습니다. export 어댑터에는 보관할 export만 담은 전용 폴더가 필요합니다.

```bash
sessioncrate archive --tool claude --older-than 30 --execute
sessioncrate archive --tool grok --older-than 30 --execute
sessioncrate archive --tool gemini --older-than 30 --execute
sessioncrate archive --tool glm --source-root /path/to/session-exports --execute
sessioncrate archive --tool agy --source-root /path/to/session-exports/agy --execute
```

export 어댑터에 사용자 홈, native 계정 폴더, 데이터베이스, 설정 폴더를 지정하지 마세요. 숨김 경로와 알려진 인증·설정 파일은 제외하고, native 저장 구조로 판별되는 경로는 거부합니다. export 정리는 지정한 export 파일의 공간만 회수하며 원래 도구의 데이터베이스 용량은 줄이지 않습니다. Claude Desktop과 Cowork는 native Claude 어댑터의 범위 밖입니다.

`scan`과 `archive`의 기본 도구는 Codex입니다. `list`·`search`·`verify`는 기본으로 모든 아카이브 영역을 조회하며, ID 없이 실행한 `prune`은 `--tool`을 지정하지 않는 한 Codex만 대상으로 합니다.

## 보관 내용 검색

```bash
sessioncrate list
sessioncrate search "결제 재시도"
sessioncrate search "결제 재시도" --tool claude
sessioncrate search "payment.retry()" --project my-app --limit 10
sessioncrate search "migration" --session ARCHIVE_ID
sessioncrate show ARCHIVE_ID --limit 30
```

검색은 대소문자를 구분하지 않는 유니코드 문자열 부분 일치입니다. 압축된 아카이브 구성원을 스트리밍으로 읽으며 원문을 복제하는 평문 검색 인덱스를 만들지 않습니다. 결과에는 아카이브 ID, 프로젝트, 원본 구성원 또는 레코드 위치, 제한된 문맥이 표시됩니다. 보관소가 크면 `--project`, `--session`, `--limit`으로 범위를 줄이세요.

알 수 없는 형식, 바이너리, 이미지, 암호화 또는 너무 큰 레코드는 검색에서 건너뛸 수 있지만 원본 바이트는 아카이브에 그대로 남습니다. JSONL 한 줄이 1MiB를 넘거나 JSON 문서가 32MiB를 넘으면 해당 텍스트 리더가 건너뛰며, 빈 결과가 중요한 경우 `skipped_records`를 확인하세요. JSON 문서의 `line`은 물리적인 파일 줄이 아니라 메시지 순번입니다.

## 검증과 복원

```bash
sessioncrate verify                 # 전체 무결성 확인
sessioncrate verify ARCHIVE_ID
sessioncrate restore ARCHIVE_ID     # 복원 미리보기

# 원래 위치를 건드리지 않고 별도 디렉터리에 복원
sessioncrate restore ARCHIVE_ID \
  --destination-home /path/to/restored --execute
sessioncrate restore ARCHIVE_ID --execute  # 기록된 원래 위치로 복원
```

`ARCHIVE_ID`에는 `list`에 표시되는 고유 접두어를 사용할 수 있습니다. SHA-256과 압축·구성원 무결성 검사가 끝난 아카이브만 복원합니다. 기존 파일의 내용이 다르면 덮어쓰지 않고, 내용이 같은 파일은 유지합니다. 지원되는 원본 형식의 경로·바이트·권한·수정 시각을 기록합니다.

`list`는 정상 항목과 manifest 오류를 함께 표시하고, `verify`는 실패 목록을 모읍니다. 관계없는 manifest가 손상되어도 정상 아카이브의 전체 ID를 사용하면 복원할 수 있습니다. 손상된 항목과 접두어가 겹칠 때는 정상 아카이브의 전체 ID 또는 명시적인 `--tool`을 사용하세요. 무결성·운영 오류는 종료 코드 `2`를 반환합니다.

묶음 복원은 목적지 근처의 비공개 임시 폴더에서 무결성을 확인한 뒤 게시합니다. 강제 종료나 전원 차단으로 `.codex-vault-restore-stage-*` 평문 임시 폴더가 남을 수 있으며 자동 수거하지 않습니다. 여러 세션 루트를 하나의 파일시스템 트랜잭션으로 교체하지는 않습니다. 압축은 암호화가 아닙니다.

## Codex 스킬과 호환성

체크아웃에서 명령과 Codex 스킬 링크를 설치할 수 있습니다.

```bash
python3 Scripts/install.py                    # 미리보기
python3 Scripts/install.py --execute
python3 Scripts/install.py --legacy-aliases --execute  # 이전 이름 링크(선택)
```

기본 설치 위치는 `~/.local/bin/sessioncrate`와 `~/.agents/skills/sessioncrate`입니다. Codex에서는 `$sessioncrate`를 호출합니다. 설치기는 기존 설치를 덮어쓰지 않으며 셸이나 Codex 설정을 수정하지 않습니다. 체크아웃 링크를 사용하면 프로젝트 폴더를 그대로 유지해야 합니다.

기존 체크아웃 실행기와 설치된 `codex-vault` 별칭은 호환성을 위해 계속 사용할 수 있습니다. 내부 `codex_vault` 모듈, v1 아카이브 ID·형식, `--codex-home` 옵션, 기본 vault 위치는 바뀌지 않습니다.

- `$XDG_DATA_HOME/codex-vault`, 없으면 `~/.local/share/codex-vault`
- `--vault PATH`로 명시적인 보관 디렉터리 선택
- 이름 변경으로 기존 아카이브·데이터베이스·인덱스를 이동하거나 마이그레이션하지 않음

## 개발·검증

```bash
bash Scripts/verify.sh
python Scripts/roundtrip_check.py
```

테스트는 임시 합성 기록만 사용합니다. 개인 기록, 계정 설정, 인증 자료를 fixture로 사용하거나 이슈에 첨부하지 마세요. 지원 플랫폼과 바이트 수준 복원 및 native 애플리케이션 동작의 차이는 [지원 범위와 검증 근거](docs/compatibility.md)에 정리되어 있습니다.

MIT License.
