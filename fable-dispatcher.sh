#!/bin/zsh
# Диспетчер «Руки Фейбла»: забирает голосовые задания Винели из issues репо shtab
# (label fable-task, только от vineli46) и выполняет их через claude -p.
# Запускается launchd-задачей com.shtab.fable каждые 60 секунд.

export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
REPO="vineli46/shtab"
OWNER="vineli46"
DIR="$HOME/штаб"
LOCK="$DIR/.fable.lock"
LOG="$DIR/dispatcher.log"

CLAUDE=$(command -v claude)
if [ -z "$CLAUDE" ]; then
  CLAUDE=$(ls -d "$HOME"/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | tail -1)
fi
if [ -z "$CLAUDE" ]; then
  echo "$(date '+%F %T') ОШИБКА: claude не найден" >> "$LOG"
  exit 1
fi

# один диспетчер за раз; лок старше 90 минут считаем протухшим
if [ -f "$LOCK" ]; then
  if [ -n "$(find "$LOCK" -mmin +90 2>/dev/null)" ]; then rm -f "$LOCK"; else exit 0; fi
fi
touch "$LOCK"
trap 'rm -f "$LOCK"' EXIT

gh issue list -R "$REPO" --label fable-task --state open \
  --json number,author --jq ".[] | select(.author.login==\"$OWNER\") | .number" 2>>"$LOG" \
| while read -r num; do
  [ -z "$num" ] && continue
  body=$(gh issue view "$num" -R "$REPO" --json body --jq .body 2>>"$LOG")
  [ -z "$body" ] && continue
  echo "$(date '+%F %T') issue #$num: старт: $body" >> "$LOG"
  gh issue edit "$num" -R "$REPO" --add-label working >> "$LOG" 2>&1

  prompt="Голосовое задание от Винели, передано со страницы «Штаб» (issue #$num). Выполни его самостоятельно от начала до конца, без уточняющих вопросов — если чего-то не хватает, прими разумное решение сама и упомяни это в отчёте. В конце ответь ОДНИМ коротким абзацем по-русски — это отчёт, который Джарвис озвучит Винели вслух (без списков, без код-блоков, без английских слов). Задание: $body"

  result=$(cd "$HOME" && "$CLAUDE" -p "$prompt" --permission-mode acceptEdits 2>>"$LOG" | tail -c 2800)
  [ -z "$result" ] && result="Я взялась за задание, но отчёт не сформировался. Загляните в лог диспетчера на маке."

  gh issue comment "$num" -R "$REPO" --body "🤖 $result" >> "$LOG" 2>&1
  gh issue close "$num" -R "$REPO" >> "$LOG" 2>&1
  echo "$(date '+%F %T') issue #$num: готово" >> "$LOG"
done

rm -f "$LOCK"
