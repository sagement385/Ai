# v15 AI Copilot

- 웹 `/ai-copilot` 탭 추가
- OpenAI 및 OpenAI-compatible, OpenRouter, Groq, DeepSeek, Ollama, offline provider 설정 추가
- 종목 분석, 백테스트 진단, 전략 개선, 코드 패치 제안 체크박스 UI 추가
- AI 결과 리포트 `reports/output/ai_copilot_result.html` 생성
- 구조화된 JSON 패치 제안 저장 `reports/output/ai_copilot_patch_proposal.json`
- 사용자 승인 후 자동 백업 및 패치 적용 기능 추가
- `.env`, `data`, `reports/output`, `backups`, `.venv` 수정 차단
