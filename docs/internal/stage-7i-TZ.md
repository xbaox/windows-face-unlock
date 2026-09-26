# ПОД-ТЗ — ЭТАП 7-i (жест-раунд в CP) + ВСТРОЕННЫЙ ХЕНДОФФ ЭТАПА 7

> **Единый файл на оба окна.** Живёт в корне репо (закоммичен **принудительно поверх `.gitignore`** — паттерн `stage-*-TZ.md` его игнорирует, добавлен через `git add -f`; тот же класс, что `face-unlock-MASTER-TZ.md` и `audit-notes.md`: перечислены в `.gitignore`, но трекаются, поэтому записи для них инертны) — прикладывать всегда **свежим из репо**, вместе с `face-unlock-MASTER-TZ.md` и `audit-notes.md` (тоже из репо).
> **Порядок:** окно 7-i (раздел §1) → окно Этапа 7 (раздел §2). На closeout'е окна 7-i архитектор ТАМ вписывает статус 7-i в промпт §2 и отдаёт его Bao — **возврата в окно Этапа 6 не требуется.** Если 7-i решено пропустить — промпт §2 берётся как есть с пометкой «7-i ПРОПУЩЕН».
> База: `master` `b3ead4a` (Этапы 0–6 DONE, влиты, запушено). Рекомендация: 7-i ДО Этапа 7 — System Protection на C: ещё включена и страхует registration-риск.

---

# §1 — ОКНО 7-i: жест-раунд в Credential Provider (C++)

## Процесс
Кодит **Claude Code (CC)**, прямой доступ к `C:\dev\windows-face-unlock`. **Архитектор** (Claude в окне) пишет промпты под CC и ревьюит. **Bao** релеит и делает всё живьём. Bao: по-русски, на «ты», кратко; **код Bao НЕ отдавать — только промпты для CC**. Рантайм: recon → «ОК» Bao → код. **Живые команды — ВСЕГДА с нуля** (полный `cd C:\dev\windows-face-unlock`, `.\` перед `.venv\Scripts\python.exe`). **CC не запускает рантайм** (сервис/камера/вход/reboot/`regsvr32`) — только код + read-only recon. Живая машина → Bao; репо → CC. Окно CC — новое. Ветка `stage7i-gesture`.

## Цель
Полный жест-раунд на локскрине: плитка просит моргнуть / повернуть голову → ждёт → верифицирует через **существующий** challenge-механизм сервиса. Итог: **paranoid = боевой daily-driver** (сейчас пассивный verify → `NEEDS_GESTURE` → `match:false` → PIN); fast выигрывает эскалацию в жест при `margin < 0.10` вместо отката в PIN.

## Рамки (жёсткие)
- **Единственное окно с санкцией на C++ CP-логику.** Числа НЕ трогать: `0.32`, `STRONG_MARGIN=0.10`, все liveness/lockout/adaptive/low-light/camera/watchdog-константы, `enroll_qc`-гейты. Жест ездит на существующем challenge-стабе — **новых порогов не вводить**.
- Серверный периметр Этапа 4 (`_build_pipe_sa` / `FIRST_PIPE_INSTANCE` / `unlock`-гейт / custody) — только по явной санкции Bao. Новая пайп-команда для жеста → **СНАЧАЛА дизайн-ревью архитектора** (инварианты: DoS-команды режет DACL; `unlock` гейтится SYSTEM; challenge не должен стать каналом выдачи пароля мимо гейта).
- Фолбэк PIN/пароль жив всегда; CP строго аддитивен; RDP-off не трогать.
- `kUnlockTimeoutMs = 12000` (`credential_provider/PipeClient.cpp:477`): вписаться в бюджет ИЛИ расширить **осознанно** (решение окна, с ревью и записью в audit-notes).
- System Protection на C: **включена** — не трогать (выключение = финал Этапа 7).
- `verify_frame` / `_prep_cuda_dlls` — байт-в-байт. EOL LF (CR считать **байтами**: `tr -cd '\r' | wc -c`), `git diff` перед коммитом, состязательное ревью-гейт держать (в Этапе 6 систематически ловил додуманные обоснования — audit-notes §F).

## Карта сборки CP (проверено живьём: Фаза B + финальный ребут closeout)
- Собирать **ТОЛЬКО в `build-cp`**: LogonUI грузит `C:\dev\windows-face-unlock\build-cp\Release\FaceCredentialProvider.dll` (сверено `reg query` HKLM CLSID `{8414D7B6-…}`). Репо-ссылки на `credential_provider\build\` — путь-дрейф, врут (6 файлов, долг Этапа 7 — здесь НЕ чинить, audit-notes §J).
- cmake бандленный: `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe` (3.31.6, MSVC 14.44). Мультиконфиг → `--config Release` **обязателен**; `-DCMAKE_BUILD_TYPE` НЕ передавать.
- Тесты C++: каталог `build-tests`, `test_parser.exe` — 16/16 на базе. Парс challenge-ответов покрыть там же.
- Length DLL может не дрогнуть — **сигнал пересборки = mtime**.
- Чистая пересборка (тот же путь+GUID) пере-`regsvr32` НЕ требует. Если регистрация меняется — вручную, элевированно; `register.ps1` НЕ использовать (битые дефолты: `build\` → exit 1; нет elevation-check; `regsvr32` — GUI-subsystem, `$LASTEXITCODE` бесполезен; долг Этапа 7).
- **После ЛЮБОЙ перерегистрации — проверка фолбэка:** убить сервис → вход PIN/паролем обязан работать.
- Билд-каталоги в `.gitignore` (с `d930a8f`); `build-cp` на диске НЕ удалять — боевая DLL.

## Ориентировка (живое состояние на закрытии Этапа 6)
- **Матч:** paranoid — жест ВСЕГДА → пассивный verify = `NEEDS_GESTURE` → `match:false` (при `real:true`). fast — challenge при сомнении (`margin < 0.10`); чистый матч fast = `distance ≤ 0.22`. `match:false` при `distance < 0.32` — margin-механика, НЕ баг (audit-notes §I). Наушники поднимают distance — на чистых прогонах снимать.
- **Тайминг `-AtLogOn`:** первый вход после ЛЮБОГО ребута = PIN (by design). Тесты жеста — **Win+L после прогрева**.
- **Здоровый прод = 6 процессов** (Service/Presence/Watchdog ×2; лаунчер `.venv` + воркер `Python312`), таски `PT0S`.
- **Живой конфиг:** `fast`, `adaptive_gallery=true`, `persistent_camera=true`, все `pipe_*=true` (unlock-гейт SYSTEM on), `auto_lock=false`, `verify_required=2`, `ru`. Галерея **30×512**; `adaptive.npz` отсутствует **by design** (Build стирает; копится на unlock с `distance ≤ 0.15`).
- **Камера может залипнуть сама** (audit-notes §K — расхождение с KNOWN_ISSUES §1, причина не установлена): сигнатура `distance 1.0 / latency ~7200мс / matches 0` → полный сброс, не паника:
  `Stop-ScheduledTask FaceUnlock-Service; Stop-ScheduledTask FaceUnlock-Presence; Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'face_service|presence_monitor' } | % { Stop-Process -Id $_.ProcessId -Force }; Start-Sleep 5; Start-ScheduledTask FaceUnlock-Service; Start-Sleep 20; Start-ScheduledTask FaceUnlock-Presence` → ~15с → `.\.venv\Scripts\python.exe tools\pipe_client.py verify` в объектив.
- Счёт процессов: `Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'face_service|presence_monitor|watchdog' } | Select-Object ProcessId, CreationDate, CommandLine | Format-List`.

## Скоуп (уточняется recon'ом)
1. **Recon (read-only):** challenge-стаб сервиса (команда/протокол/вердикты), CP-флоу (`FaceCredential.cpp` / `PipeClient.cpp` — состояния плитки, тексты, тайминги), реакция CP на `NEEDS_GESTURE` сейчас.
2. **Дизайн (ревью у архитектора ДО кода):** протокол жест-раунда через пайп; бюджет 12с или расширение; UX-тексты RU/EN; поведение на таймаут/отказ жеста (чистый откат в PIN; lockout-нейтральность обсудить).
3. **Реализация:** сервис-сторона (доводка стаба при нужде — с оглядкой на периметр), CP-сторона, тесты парсера.
4. **Живой прогон (Bao):** пересборка → (пере)регистрация при необходимости → paranoid проходит жестом с локскрина; fast эскалирует при сомнении; фолбэк цел.
5. **Closeout — см. хвост §1 ниже.**

## Приёмка (gate)
- paranoid: вход лицом+жест с реального локскрина — стабильно, повторяемо.
- fast: `margin < 0.10` → жест-эскалация вместо PIN (в бюджете).
- Kill сервиса → PIN работает; unregister чист (однократно проверить и вернуть).
- Заморозки целы (git-сверка: числа / периметр / `verify_frame`).
- Аудит-лог пишет challenge-исходы.

## Closeout окна 7-i (обязательный хвост; исполняет архитектор окна 7-i)
1. Секция «Этап 7-i» в `audit-notes.md` (глубина по образцу Этапа 6).
2. Мерж `stage7i-gesture` → master (тип — по ситуации), пуш.
3. **Вписать статус 7-i в промпт §2 этого файла** (плейсхолдер `[СТАТУС 7-i]`) — прямо в репо-файле, коммитом — и сказать Bao: к окну Этапа 7 приложить `MASTER` + `audit-notes` + `stage-7i-TZ.md`, все свежие из репо. Возврат в старые окна не нужен.

## Самодостаточный промпт окна 7-i (Bao вставляет первым сообщением)

```
Прокачиваю опенсорс caochitam/windows-face-unlock (face-логин с настоящим Credential Provider) до уровня Windows Hello на ASUS TUF FA507XI — Win11, Python 3.12, RTX 4070, RGB-вебка без ИК, вход по PIN. Форк xbaox/windows-face-unlock, локально C:\dev\windows-face-unlock.
Приложены: face-unlock-MASTER-TZ.md, stage-7i-TZ.md (читай ПЕРВЫМ, твой раздел §1; §2 — хендофф следующего этапа, его отдашь на closeout), audit-notes.md (секция «Этап 6»: §G live-итоги, §I margin, §J путь-дрейф, §K камера, §F ревью-гейт).
Процесс: кодит Claude Code (CC, НОВОЕ окно, прямой доступ к репо). Ты — архитектор: промпты для CC + ревью. Я релею и делаю всё живьём. Мне по-русски, на «ты», кратко, код НЕ давай — только промпты для CC. Рантайм: recon → моё ОК → код. Живые команды всегда с нуля (полный cd C:\dev\windows-face-unlock, .\ перед .venv\Scripts\python.exe). CC рантайм не запускает (regsvr32/сервис/камера/вход/ребут — я сам).
Состояние: Этапы 0–6 DONE, master b3ead4a (или новее — сверься), запушено. Здоровый прод = 6 процессов (Service/Presence/Watchdog ×2, PT0S). Живой конфиг: fast, adaptive_gallery=true, persistent_camera=true, pipe_*=true, auto_lock=false, verify_required=2, ru. Галерея 30×512; adaptive.npz отсутствует by design.
Задача окна: жест-раунд в C++ CP — плитка просит моргнуть/повернуть → ждёт → верифицирует через СУЩЕСТВУЮЩИЙ challenge-стаб сервиса. После — paranoid боевой daily-driver. ЕДИНСТВЕННОЕ окно с санкцией на C++ CP-логику; registration-риск изолирован тут.
Жёсткие рамки: числа НЕ трогать (0.32, STRONG_MARGIN 0.10, все константы liveness/lockout/adaptive/low-light/camera/watchdog, enroll_qc); новых порогов не вводить; серверный периметр Этапа 4 — только по моей явной санкции (новая пайп-команда → сначала дизайн-ревью; challenge не канал пароля мимо SYSTEM-гейта); фолбэк PIN всегда жив; kUnlockTimeoutMs=12000 — вписаться или расширить осознанно; System Protection включена — не трогать; verify_frame/_prep_cuda_dlls байт-в-байт; ветка stage7i-gesture; EOL LF (CR байтами); git diff перед коммитом; ревью-гейт держать.
Карта сборки — в §1 stage-7i-TZ: build-cp ТОЛЬКО (доки врут про credential_provider\build — долг Этапа 7, не чинить), бандленный cmake с --config Release (без -DCMAKE_BUILD_TYPE), mtime = сигнал, register.ps1 НЕ юзать, после перерегистрации проверять фолбэк PIN.
Тайминг: первый вход после ЛЮБОГО ребута = PIN (by design), тесты — Win+L после прогрева. match:false при distance<0.32 = margin-механика; чистый матч fast = distance≤0.22; наушники поднимают distance. Камера может залипнуть (distance 1.0 / ~7200мс) → полный сброс, команда в §1.
На closeout окна: секция 7-i в audit-notes, мерж+пуш, вписать статус в промпт §2 stage-7i-TZ.md коммитом, сказать мне что прикладывать к окну Этапа 7.
Начни: (1) статус-чек — git log --oneline -3 master, git status, счёт процессов (жду 6); (2) промпт CC на recon challenge-стаба + CP-флоу (read-only) → покажи протокол/состояния/тексты как есть → дизайн-ревью → жди моих подтверждений на рантайм-шагах.
```

---

# §2 — ХЕНДОФФ ЭТАПА 7 (упаковка / подпись / uninstaller / чеклист)

> Выдаётся из окна 7-i (статус вписан в промпт ниже). Первоисточник долгового реестра — **audit-notes «Этап 6» §J–M** (файл всегда приложен): §J путь-дрейф CP-DLL + register.ps1; §K камера (вынос визарда + координация владения + open-deadline + lease + KNOWN_ISSUES §1) + auto_lock + A3-field; §L installer/config-дрейф (config.example 0.45→0.32, verify_required 3→2, 36 отсутствующих ключей, deepface-спека, startuptray, 4 регистратора); §M TODO Этапа 3 (тихий watchdog, service.py 1110 строк, EOL). Здесь — скоуп ядра + дельты СВЕРХ §J–M + приёмка + промпт.

## Скоуп ядра (MASTER §3 «Упаковка», §5 строка 7, §7)
1. Подпись DLL: мин. self-signed + инструкция под реальный сертификат.
2. Валидация схемы конфига — полнота на все 48 полей `Config` (fail-loud с Этапа 4 есть).
3. Структурные логи (формат/ротация; `audit_max_mb` есть — сверить остальные).
4. Апдейт-механизм.
5. Чистый uninstaller: CP-unregister, **все ТРИ таски** (Service/Presence/Watchdog), `.venv`, `~/.face-unlock/` — без следов; вход паролем цел на каждом шаге.
6. Доки: сверка INSTALL/README (**эталон — `config.py`**); «первый вход после ребута = PIN» — в пользовательские доки; KNOWN_ISSUES §1 обновить по итогам диагностики камеры.
7. **Security-чеклист MASTER §7** — попунктно: сделано / снято с обоснованием.
8. **⭐ SYSTEM-custody выдачи пароля — РЕШИТЬ СУДЬБУ:** реализация здесь ИЛИ явный перенос на ревизию Этапа 8 с обоснованием (merge `b3ead4a` честно держит открытым). TPM-seal — той же судьбой (без custody не мешает same-user — threat-model Этапа 4).

## Дельты СВЕРХ audit-notes §J–M
- **Watchdog ping-блипы:** единичные `ping failed (1/3)` каждые ~6.5 мин (2с-таймаут впритык; до 2/3 не доходит, рестартов ноль) → тюн `watchdog_ping_timeout_s` живым конфигом ИЛИ принять; смена дефолта — по санкции.
- **`service.py` рефакторинг** (TODO c): теперь допустим (мерж прошёл) — только отдельным осознанным шагом с полным регрессом селфтестов, не смешивая с функциональными коммитами.
- **Git-хаускипинг:** `git branch -d stage6-ux` (влита, держали для референса) и `stage7i-gesture` после мержа 7-i; в `.gitignore` дубль `stage-2-RESUME.md` + инертные tracked-записи; `stage-2-TZ.md` в корне — исторический, кандидат на удаление (**`stage1-benchmark.md` НЕ трогать** — базовые числа стока, на них ссылается приёмка Этапа 1 в MASTER).
- **i18n:** 10 языков EN-фолбэк (en/ru полные) — принять или добить, низкий приоритет.
- **Пост-7-i:** если жест-раунд менял CP — сверить CP-README/доки; таймаут CP, если расширяли — отразить в доках.

## Приёмка (gate; MASTER §5 строка 7 + §7)
- Установка на чистую машину (или честная эмуляция): из коробки; конфиг-пример честный; веса правильные (buffalo_l, не deepface).
- Удаление: ноль следов; вход паролем цел на каждом шаге.
- Security-чеклист §7 пройден попунктно.
- Подпись стоит; инструкция под реальный сертификат написана.
- Долги §J–M + дельты: закрыты ИЛИ явно приняты с записью в audit-notes (ревизия принятых — Этап 8).
- **ФИНАЛ: напомнить Bao выключить System Protection на C:** — ТОЛЬКО когда всё собрано и протестировано; явный последний пункт приёмки (сквозное напоминание из шапки audit-notes — Этап 7 обязан исполнить).

## Приложить к окну Этапа 7
`face-unlock-MASTER-TZ.md` + `stage-7i-TZ.md` (этот файл, статус вписан) + `audit-notes.md` — все свежие из репо.

## Самодостаточный промпт окна Этапа 7 (Bao вставляет первым сообщением)

```
Прокачиваю опенсорс caochitam/windows-face-unlock (face-логин с настоящим Credential Provider) до уровня Windows Hello на ASUS TUF FA507XI — Win11, Python 3.12, RTX 4070, RGB-вебка без ИК, вход по PIN. Форк xbaox/windows-face-unlock, локально C:\dev\windows-face-unlock.
Приложены: face-unlock-MASTER-TZ.md, stage-7i-TZ.md (твой раздел — §2: скоуп ядра + дельты; §1 — история окна 7-i), audit-notes.md (§J–M — первоисточник долгового реестра; секция 7-i — если был).
СТАТУС 7-i: DONE — жест-раунд реализован и принят вживую (merge 231ca39); paranoid — боевой daily-driver; C++ CP снова ЗАМОРОЖЕН (санкция 7-i исчерпана); LEFT_IS_NEGATIVE_YAW=False — живая калибровка 2026-07-27; fast-эскалация вживую отложена (рецепт в audit-notes «Этап 7-i»), ревизия на Этапе 8.
Процесс: кодит Claude Code (CC, НОВОЕ окно). Ты — архитектор: промпты для CC + ревью. Я релею живьём. Мне по-русски, на «ты», кратко, код НЕ давай — только промпты для CC. Recon → моё ОК → код. Живые команды всегда с нуля (полный cd C:\dev\windows-face-unlock, .\ перед .venv\Scripts\python.exe). CC рантайм не запускает.
Состояние: Этапы 0–6 DONE, master b3ead4a или новее (сверься). Здоровый прод = 6 процессов (Service/Presence/Watchdog ×2, PT0S). Живой конфиг: fast, adaptive_gallery=true, persistent_camera=true, pipe_*=true, auto_lock=false, verify_required=2, ru. Галерея 30×512; adaptive.npz отсутствует by design.
Задача этапа: упаковка/подпись/uninstaller/доки + security-чеклист MASTER §7 + отработка долгового реестра: §J–M audit-notes (камера: вынос визарда + координация владения + диагностика idle-залипания + open-deadline + lease; installer: config.example 0.45→0.32 и verify_required 3→2, 36 отсутствующих ключей, deepface-спека, путь-дрейф CP-DLL 6 файлов, register.ps1, консолидация 4 регистраторов; поведение: auto_lock-тюнинг, A3 field, тихий watchdog) + дельты §2 stage-7i-TZ (ping-блипы, service.py-рефакторинг осознанно, git-хаускипинг, i18n, пост-7-i sync). Плюс РЕШИТЬ СУДЬБУ SYSTEM-custody: реализация здесь ИЛИ явный перенос на Этап 8 с обоснованием (TPM-seal той же судьбой).
Заморозки: числа (0.32, STRONG_MARGIN 0.10, liveness/lockout/adaptive/low-light/camera/watchdog, enroll_qc), verify_frame/_prep_cuda_dlls байт-в-байт, серверный периметр Этапа 4 — только по моей санкции; C++ CP-логика — по статусу 7-i выше; дефолты config.py — по санкции поштучно (adaptive_gallery=False и auto_lock=True — осознанные решения Этапа 6, audit-notes §H/§K). EOL LF (CR байтами), git diff перед коммитом, ревью-гейт держать, селфтесты на .venv 3.12, ветка stage7-packaging. Этап крупный — дели на блоки с промежуточными handoff'ами по образцу 6a–6e.
System Protection на C: ВКЛЮЧЕНА — выключение ТОЛЬКО в самом конце, явным последним пунктом приёмки, после полной сборки и тестов; напомни мне сам, когда дойдём (сквозное из шапки audit-notes).
Тайминг: первый вход после ЛЮБОГО ребута = PIN (by design). Камера может залипнуть (distance 1.0 / ~7200мс) → полный сброс (команда в §1 stage-7i-TZ).
Начни: статус-чек (git log --oneline -5 master, git status, процессы — жду 6) + план блоков реестра (что первым, что группируется, где живые прогоны) → покажи → жди моих подтверждений.
```
