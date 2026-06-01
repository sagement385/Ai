# GitHub 업로드 가이드

이 폴더는 GitHub에 올리기 좋게 정리된 버전입니다.

## 포함한 것

- 실제 Python 소스 코드
- 설정 예시 파일 `.env.example`
- 문서 `docs/`, `CHANGELOG_*.md`
- 실행에 필요한 `requirements.txt`
- GitHub용 `.gitignore`
- 빈 데이터/리포트 폴더 유지를 위한 `.gitkeep`

## 제외한 것

- `__pycache__/`
- `*.pyc`
- `.env`
- 토큰/비밀키 파일
- 로컬 DB, 캐시, 로그, zip 파일
- 생성 리포트/차트/포트폴리오 산출물

## GitHub 웹에서 올리는 법

1. GitHub에서 새 저장소를 만듭니다.
2. 가능하면 처음에는 `Private`로 만듭니다.
3. 이 폴더 안의 파일과 폴더를 모두 드래그해서 업로드합니다.
4. `Commit changes`를 누릅니다.

주의: 이 폴더 자체를 zip으로 올리는 것보다, 압축을 푼 뒤 내부 파일/폴더를 업로드하는 것이 좋습니다.

## 로컬 실행

```bash
pip install -r requirements.txt
python main.py serve
```

브라우저에서 아래 주소로 접속합니다.

```text
http://127.0.0.1:8765
```

## API 키 설정

`.env.example`을 복사해서 `.env`로 만든 뒤 값을 입력합니다.

```bash
cp .env.example .env
```

Windows CMD에서는:

```cmd
copy .env.example .env
```

`.env`는 `.gitignore`에 의해 GitHub에 올라가지 않도록 설정되어 있습니다.
