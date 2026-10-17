# Публикация на GitHub

Публичный репозиторий проекта: [Holyandra/AI-Limits](https://github.com/Holyandra/AI-Limits).

## Сведения для карточки репозитория

**Description:** `A lightweight Windows tray widget for viewing Codex and Antigravity quota limits.`

**Topics:** `windows`, `python`, `pyside6`, `system-tray`, `codex`, `antigravity`, `quota-monitor`

## Публикация обновлений

После внесения изменений проверьте список файлов и отправьте коммит:

```powershell
git status --short
git diff --check
git add .
git diff --cached --check
git status --short
git commit -m "Describe the change"
git push
```

`.gitignore` исключает локальные ярлыки, среды Python, кэш, логи и превью. Не добавляйте снимки экрана с реальными квотами или личные данные.

Репозиторий содержит исходный код, а не готовый установщик. Для запуска нужны Python 3.12 и зависимости из `requirements.txt`; инструкция есть в README.
