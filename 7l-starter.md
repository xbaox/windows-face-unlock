# 7l-starter — финальная миля Этапа 7 (handoff из окна 7k)

## Роли и протокол (без изменений + два новых правила)
- Claude — архитектор: решения, промпты CC, живые PowerShell-блоки. CC — только репо, рантайм никогда. Bao — реле и весь рантайм. Блоки по одному, $go-самоблокировка, гейт перед каждым шагом, архив перед деструктивом.
- **НОВОЕ (канал, 7k):** вложения Bao→Claude не доходят — потеряно ~7 отчётов, файлы на стороне Claude физически пустые (песочница проверялась многократно). Весь вывод — текстом в теле сообщения, выжимками ≤ ~15 коротких строк. Каждый рантайм-блок обязан дублировать вердикт на диск (`C:\dev\d-series\<окно>\...-verdict.txt`).
- **НОВОЕ (роли, 7k):** архитектор читает запушенный код сам с github.com/xbaox/windows-face-unlock (origin/stage7-packaging = `e6292e0`). Непушенные локальные правки — по-прежнему только через CC.

## Канон на закрытие 7k
- Репо: ветка `stage7-packaging`, HEAD **`b7d9ab7`** (ahead 3 от origin/`e6292e0`, push не делался):
  `b7d9ab7` мина-3 insightface objects → `2d2b55d` мина-2 scipy._external → `b627766` мина-1 scipy._cyutility → `e6292e0` (origin).
- Диаг-ветка `diag/7j2-debuglog`: `e260df5` (exc_info на recognizer:446) → `9aaa3f7` (DEBUG-уровень) → `b7d9ab7`. NEVER MERGE.
- Инсталлер-канон: **SHA-256 `717040B6`**, 1 625 091 796 B, `C:\dev\windows-face-unlock\installer_output\WindowsFaceUnlock-Setup-0.1.0.exe`. Собран `installer\build.py` (шаги 5/5; строка EXIT потеряна каналом — канон принят по артефакт-гейту: свежий exe, стабильный размер, dist-цепочка 18:40→18:55; Inno CRC-валидирует пакет при установке). Прежний канон `2E56F179` замещён.
- **build.py лежит в `installer\build.py`, НЕ в корне репо** (спотык блока 7k-6; подтверждено по клону origin).
- dist (расходник, в Inno не идёт): три exe от 18:40, `_internal\objects\meanshape_68.pkl` 974 B на месте.
- Рантайм: PF-таски `*FaceUnlock*` указывают на **установленную копию эпохи 2E56F179** (слепую: presence absent, d=n/a error), НЕ на dist. Путь установки по `installer\installer.iss:27` — `{autopf}\<ShortName>`, per-user ожидаемо `%LOCALAPPDATA%\Programs\WindowsFaceUnlock\` (подтвердить в 7l читалкой C1 без обрезки хвоста пути). dist-смоки валидны только явным запуском exe — все блоки 7k так и делали.
- Конфиг `C:\Users\zabao\.face-unlock\config.toml`: L52 `auto_lock = false`, L54 `debug_dump_frames = false` (в 7k не менялись). System Protection на C: ВКЛ. PIN — гарантированный вход (Invariant §1).
- Улики: `C:\dev\d-series\7k\` — пред-архивы service.log (pre-7k3 / 3bis / 3ter / 7k7), серии probe/presence/shutdown, `ready-status`/`rs2`, `7k7d-verdict.txt` (**HITS=2 CNT2=0 GREEN=False**), `presence-final1..3.txt` (№1 — **чистый PASS**: present/real/strong за 111 мс). E-улики смока 20:21 восстановимы grep'ом `'presence probe'` по сегменту service.log после якоря `service.pre-7k7.log`.
- PF-таски `*FaceUnlock*` остановлены и ОТКЛЮЧЕНЫ (Disable-ScheduledTask, блок 7k-off, вердикт `C:\dev\d-series\7k\off-verdict.txt`): PROC=0 — норма, нагрузки нет, вход по PIN; recovery-ветки со Start-ScheduledTask не сработают до Enable; включение — инсталлером на миле 7l (register_tasks) либо Enable-ScheduledTask вручную.

## Итог 7k — статус KNOWN_ISSUES §5
1. Три мины найдены, убиты, подтверждены прогонами. Классы: **(1)** импорт по имени внутри бинарных `.pyd` (`scipy._cyutility`, `b627766`); **(2)** динамический `__import__` со склейкой строк (`scipy._external`, `2d2b55d`); **(3)** `sys.frozen`-ветки путей данных в вендоре (insightface `objects/` → корень бандла, контракт `pickle_object.py:13`, `b7d9ab7`).
2. Frozen распознаёт: диаг-сборка — пробы 2–6 present:true, d=0.205–0.242, verdict strong; релизная (INFO) — чистый PASS + 2/3 ночью.
3. «Красный» смок 20:21 — **не баг**: анти-экран (FFT hf текстуры кожи, `liveness.py:334–359`) дрейфует с освещением — вечером hf падает, кадр помечается suspect. `state=uncertain` требует sighting: suspect = лицо найдено, d ≤ threshold+presence_soft_margin, real=False (`service.py:345–381`) — т.е. конвейер YuNet→align→ArcFace→матч доказанно жив. Живой прецедент 19:49:46 вписан в комментарий `service.py:336`. **Смок-протокол отныне: только яркий/дневной свет, 50–80 см.**
4. NoneType-на-shutdown из 7k-3 реклассифицирован: это была мина-3, не гонка закрытия камеры.
5. Механизм §5 закрыт; остаток — формальный зелёный смок 3/3 при свете и инсталл-миля.

## Петля 7l (санкционировать при открытии)
0. Гейт-читалка: ветка/HEAD/чистота. Если дерево несёт ` M KNOWN_ISSUES.md` + ` M audit-notes.md` — это Фаза A док-коммита CC-7k-6: ревью диффа → Фаза B. Микро-чтение `presence-final2.txt`/`final3.txt`: какая проба финального смока не дотянула и чем (uncertain / пайп / ошибка).
1. Смок при дневном/ярком свете: каркас 7k-7d без E-шага; критерий 3/3 present; вердикт на диск.
2. Зелень → инсталл: стоп-канон → счёт 0 → `717040B6` с `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG` → ждать `Log closed`; стек поднимает сам инсталлер (register_tasks).
3. Приёмка: два PID-снапшота по 60 с; mtime service.log > t(install); 3 пробы present:true; verify (NEEDS_GESTURE при марже < 0.10 — здоровый исход); подтвердить, что PF-таски указывают на **новую** копию.
4. Финал: акт по KNOWN_ISSUES §5, push `stage7-packaging` (по отдельной санкции), решение о merge в master.

## Долги Этапа 8 (добавлено в 7k)
- Guard на None-кадр в warmup/get-пути; exc_info в `recognizer.py:446` для релиза (сейчас INFO слеп к причинам отказов) — обсудить формат.
- Анти-экран: luma-aware порог hf / вечерний режим — замороженные константы не трогать без акта.
- Класс-гард сборки: grep вендорных site-packages на `getattr(sys, 'frozen'` / `_MEIPASS` (мины класса 3).
- Смок-протокол в доки (яркий свет, посадка, критерий).
- Канал вложений: репорт бага в поддержку Anthropic (файлы приходят пустыми).
