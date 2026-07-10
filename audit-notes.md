# audit-notes.md — Этап 0, аудит стока `caochitam/windows-face-unlock`

> Форк: `xbaox/windows-face-unlock` · ветка `stage0-audit` · дата: 2026-07-07
> Слабые места стока + карта «куда бьём» по этапам. Прикладывать к окнам следующих этапов.

\---

## ⚠️ СКВОЗНОЕ НАПОМИНАНИЕ (тащить через все этапы)

**Защита системы (System Protection / точка восстановления) на диске C: включена намеренно как страховка от лок-аута.
Отключить её можно ТОЛЬКО на Этапе 7 — и только после того, как вход лицом с локскрина собран, работает и протестирован.**
До Этапа 7 не выключать. Финальное окно (Этап 7) обязано напомнить Bao это сделать явным пунктом приёмки.

\---

## 1\. Канал (named pipe) — КРИТИЧНО → Этап 4

Файл: `face\_service/service.py`

|#|Что|Где|Риск|Куда бьём|
|-|-|-|-|-|
|C1|**NULL DACL** пайпа: `sd.SetSecurityDescriptorDacl(1, None, 0)`|`service.py:57–64` (`\_build\_sa\_everyone`), применяется `:296`|К пайпу коннектится ЛЮБОЙ локальный процесс под твоей учёткой|Этап 4: DACL = SELF (текущий юзер) + SYSTEM (LogonUI). Больше никого.|
|C2|**Нет анти-сквоттинга**: `PIPE\_UNLIMITED\_INSTANCES`|`service.py:301`|Вредонос поднимает свой пайп с тем же именем → перехват коннектов CP / фишинг|Этап 4: `FIRST\_PIPE\_INSTANCE` + проверка, что сервер — наш (мьютекс уже есть, но это не защита канала).|
|C3|**Пароль отдаётся в открытом виде** на `unlock` при матче|`service.py:280–291` (`\_handle`, ветка `unlock`)|В связке с C1: чужой процесс шлёт `unlock`, ждёт, пока ты перед камерой, забирает пароль Windows|Этап 4: nonce от CP → сервис подписывает ответ ключом, недоступным чужим; DACL (C1) отсекает чужих клиентов.|
|C4|**Нет nonce / подписи ответа** вообще|весь `\_handle`|Реплей: записал ответ сервиса — воспроизвёл|Этап 4: challenge-response, nonce одноразовый, подпись.|
|C5|Команды `shutdown`, `pause\_camera`, `resume\_camera`, `reload\_config` без авторизации|`service.py:240–260`|Любой процесс глушит сервис / уводит камеру → DoS входа лицом|Этап 4: те же DACL + подпись закрывают; опц. — allowlist команд для не-SYSTEM клиента.|

Команды сервиса (для справки, `\_handle`): `ping`, `status`, `reload\_config`, `shutdown`, `pause\_camera`, `resume\_camera`, `build\_enrollment`, `verify`, `presence`, `unlock`.

**Есть и хорошее:** single-instance mutex `Local\\FaceUnlockService` в `serve\_forever` — два сервиса не конкурируют за пайп (но это про стабильность, не про безопасность).

\---

## 2\. Хранение пароля → Этап 4 (опц. TPM)

Файл: `face\_service/credentials.py`

* Схема: DPAPI **user-scope** (`win32crypt.CryptProtectData`), блоб `{u,p,d}` в `%USERPROFILE%\\.face-unlock\\creds.dat` (см. `CREDS\_PATH`).
* **Дыра:** `ENTROPY = b"face-unlock:v1"` — захардкожен и лежит публично в опенсорсе. Дополнительная энтропия должна быть секретом; здесь она известна всем → барьер против таргетированного вредоноса **под твоей же учёткой** ≈ 0.
* Против других юзеров машины и оффлайн-атаки DPAPI user-scope держит нормально (то, что и заявлено в докстринге файла — честно).
* Куда бьём: Этап 4 (advanced) — TPM-sealing DPAPI-блоба; как минимум — вынести энтропию в машинно-уникальный секрет, а не хардкод.

\---

## 3\. Движок распознавания → Этап 1

Файлы: `face\_service/recognizer.py`, `face\_service/detector.py`, `requirements.txt`, `config.example.toml`

* Сток на **DeepFace**:

  * эмбеддинг — `DeepFace.represent(model\_name="ArcFace")` → тянет **`tf-keras` / TensorFlow** (тяжёлый импорт).
  * живость — `DeepFace.extract\_faces(anti\_spoofing=True)` → **MiniFASNet через `torch`/`torchvision`**.
* Детектор по умолчанию — `detector\_backend = "opencv"` (Хаар-каскады: медленно + неточно на углах/свете).
* **Двойной прогон детектора на каждый кадр:** в `verify\_frame` сначала `extract\_faces` (детект + анти-спуф), потом `represent` (снова детект + выравнивание + эмбеддинг). Детектор гоняется дважды по одному кадру.
* Метрика: cosine distance, `threshold = 0.45` (ниже = строже).
* Куда бьём (Этап 1): полный ONNX/InsightFace `buffalo\_l` — **YuNet detect + ArcFace embed** на onnxruntime-GPU (RTX 4070); один детект на кадр; эмбеддинг только по выровненному кропу; прогрев моделей; early-exit на первом уверенном матче; graceful CPU-фолбэк. Числа бенчмарка стока (Шаг 9) — база для сравнения.

Зависимости стока (`requirements.txt`): `deepface`, `opencv-python`, `numpy`, `tf-keras`, `pywin32`, `psutil`, `torch`, `torchvision`, `pystray`, `Pillow`, `tomli/tomli-w`. На Этапе 1 связка `deepface + tf-keras + torch` уходит в пользу `onnxruntime-gpu + insightface`.

\---

## 4\. Живость (liveness) → Этап 2

* Сейчас: только **пассивный** MiniFASNet (`is\_real`). Плоское фото держит средне, видео-реплей с телефона и статичный экран — слабо.
* Куда бьём (Этап 2): активный челлендж — blink-детект (EAR по лэндмаркам) + рандомный микро-жест как эскалация; анти-экран детектор (муар/блики/рамка); режимы **Быстрый** (челлендж при сомнении, дефолт) / **Параноик** (челлендж всегда).

\---

## 5\. Rate-limit / lockout / аудит → Этап 2

* В стоке **нет** ни rate-limit, ни lockout после N провалов лицом, ни аудит-лога попыток (есть только обычный `logging` в `%USERPROFILE%\\.face-unlock\\...log`).
* Куда бьём (Этап 2): лимит попыток + временный lockout лица → только PIN; аудит-лог каждой попытки (успех/провал, скор, вердикт liveness, время).

\---

## 6\. GUID → Шаг 4 (сейчас, Этап 0)

Захардкожен `{F8A0B4D9-3C7F-4B0A-9E21-8C1B1E2B7C10}`. Места:

МОЙ GUID: {8414D7B6-D536-461B-B31B-ADF77B3A8974}

|Файл|Строка|Форма|Менять?|
|-|-|-|-|
|`credential\_provider/guid.h`|4 (коммент), 6–7 (`DEFINE\_GUID`)|байтовая|**да**|
|`credential\_provider/dll.cpp`|42|строковая `{...}` (self-register в реестр)|**да**|
|`credential\_provider/dll.cpp`|60|строковая `{...}` (unregister)|**да**|
|`credential\_provider/FaceCredential.cpp`|141|макрос `CLSID\_FaceCredentialProvider`|нет (тянет из guid.h)|
|`credential\_provider/dll.cpp`|23|макрос|нет|

Реестр пишется самим DLL (`regsvr32` → `DllRegisterServer` в `dll.cpp`), отдельного захардкоженного реестрового пути в `register.ps1` нет — источник строкового GUID один: `dll.cpp:42,60`. Байтовая (`guid.h`) и строковая (`dll.cpp`) формы **обязаны совпадать**.

> Правка чисто текстовая. Компиляция C++ — только Этап 5. На Этапе 0 просто вписываем новый GUID корректно в оба формата, чтобы на Этапе 5 собралось.

\---

## 7\. Конфиг — knobs (`config.example.toml`)

|Knob|Дефолт|Смысл|
|-|-|-|
|`model\_name`|`ArcFace`|DeepFace-модель (уйдёт в ONNX на Этапе 1)|
|`detector\_backend`|`opencv`|детектор (→ YuNet)|
|`distance\_metric`|`cosine`|метрика|
|`threshold`|`0.45`|cosine distance, ниже = строже|
|`anti\_spoofing`|`true`|MiniFASNet|
|`camera\_index`|`0`|индекс вебки|
|`verify\_frames`|`5`|кадров на verify|
|`verify\_required`|`3`|матчей из verify\_frames для успеха|
|`presence\_interval\_s`|`60`|период проверки присутствия|
|`presence\_absent\_strikes`|`2`|«страйков» отсутствия до авто-лока|
|`presence\_mode`|`recognition`|`recognition` (должен совпасть) / `detection` (любое лицо)|
|`warmup\_on\_start`|`true`|прогрев моделей при старте|

\---

## 8\. Карта «куда бьём» (сводно)

* **Шаг 4 (сейчас):** свой GUID → `guid.h` + `dll.cpp:42,60`.
* **Этап 1:** движок ONNX/InsightFace (YuNet+ArcFace, GPU), один детект, early-exit, CPU-фолбэк. База — числа бенчмарка стока (Шаг 9).
* **Этап 2:** активный liveness (blink+жест), анти-экран, rate-limit+lockout, аудит-лог, режимы Быстрый/Параноик.
* **Этап 3:** надёжность (мульти-условный энролл, адаптивная галерея, низкий свет, занятая камера, watchdog).
* **Этап 4 (DONE ✅):** харднинг канала — явный DACL `SELF+SYSTEM` + Medium mandatory-label (C1), `FIRST_PIPE_INSTANCE` + проверка server-SID клиентом (C2), SID-гейт `unlock`→SYSTEM (C3/C5; дефолт OFF, активируется в Этапе 5), per-install энтропия + авто-миграция v1→v2 (§2). **nonce/подпись (C3,C4) СНЯТЫ** как избыточные к OS-границе (обоснование — секция «Этап 4 … Threat-model (пересмотр)»). DoS-команды (C5) режет DACL. Остаток → Этап 5.
* **Этап 5:** Credential Provider (C++, VS2022+CMake), свой GUID, защищённый протокол, таймаут, фолбэк, RDP-off, регистрация. ⚠️ ЛОКСКРИН. **Перенесено из Этапа 4:** SYSTEM-custody выдачи пароля (доминирующий same-user риск), флип `pipe_unlock_require_system=true`, опц. TPM-seal.
* **Этап 6:** UX (визард PySide6, трей, RU/EN).
* **Этап 7:** упаковка/подпись/uninstaller/чеклист + **напомнить Bao выключить System Protection**.


> Свой GUID (Шаг 4): {8414D7B6-D536-461B-B31B-ADF77B3A8974}

\---

## Этап 3 — надёжность движка (Stage 3 — engine reliability) — DONE ✅

> Ветка `stage3-reliability` → слита в `master` (`--no-ff`). Дата: 2026-07-08.
> Коммиты: `8c30224` (Steps 0–2: QC-энролл + adaptive + Stage-2 tail), `1dc496c` (Step 3: low-light detect + too-dark), `c050f16` (Step 3.3: gated exposure-boost), `25bbccc` (Step 4: camera-busy), `d54a93d` (Step 5: watchdog + graceful-shutdown); `259d30e` — гигиена (LF-pin `config.py`/`service.py`, ignore `.idea`/бинарники).

**Что сделано по шагам:**

- **Step 1 — QC-энролл** (`enroll_qc.py`, `recognizer.enroll_from_dir`): каждый кадр проходит гейты `det_score` / sharpness (variance-of-Laplacian на 112-кропе) / luma ДО эмбеддинга; если QC-прошедших `< enroll_min_frames` → чёткий `RuntimeError` с причинами (а не тихая слабая галерея). Per-frame лог + read-only `tools.enroll_qc_probe`.
- **Step 2 — адаптивная галерея + анти-отравление** (`adaptive.py`, `recognizer.maybe_adapt`, `service._maybe_adapt_gallery`): opt-in, по умолчанию **OFF**. Just-verified эмбеддинг добавляется ТОЛЬКО если его дистанция к enrollment-базе ≤ `threshold − adaptive_margin`, liveness прошёл, нет screen-флага, (в параноике) есть жест; FIFO-cap + cooldown. Ceiling якорится на enrollment (нет drift-hopping); спуф/чужой/replay-of-self галерею не травят.
- **Step 3 — low-light детект + too-dark гейт** (`lowlight.py`, `service.py`): `scene_luma` по всему кадру; ниже `low_light_luma_min` unlock честно отказывает с reason `too-dark` — **LOCKOUT-НЕЙТРАЛЬНО** (тьма — среда, не провал матча), без тихого false-reject.
- **Step 3.3 — gated exposure-boost** (`camera_boost.py`, `service._maybe_boost` + `service._analyze_burst`): только НИЖЕ флора поднимает CAP_PROP_EXPOSURE и переснимает бёрст перед too-dark-отказом; exposure всегда восстанавливается (`finally`); если всё ещё темно — остаётся too-dark, нейтрально к lockout. Живой смоук: scene 44→115, restore подтверждён.
- **Step 4 — camera-busy** (`camera_open.py`, `service._acquire_camera`): когда камеру держит ЧУЖОЙ процесс (не наш enrollment-lease) — ограниченный retry-loop вместо многосекундного `exception:…`; reason `camera-busy`, **LOCKOUT-НЕЙТРАЛЬНО**; отличается от enrollment-lease. Юнит-доказано; реальная интеграция на этой вебке self-skip (cv2 не держит устройство эксклюзивно).
- **Step 5 — watchdog + graceful-shutdown** (`watchdog.py`, `service.py`): внешний Scheduled-Task пингует пайп; N провалов подряд → рестарт kill-then-start (зависший-но-живой процесс держит mutex); намеренный `shutdown` роняет self-expiring pause (watchdog не воскрешает намеренный стоп; expired pause самоудаляется). Graceful-shutdown чинит error 233. Живой смоук: shutdown без 233, Ctrl+C чисто, рестарт убитого сервиса.

**Обоснование правок `service.py` (критерий 7)** — все по делу шагов выше, ничего лишнего:

| Шаг | Добавлено в `service.py` | Зачем |
|-|-|-|
| Step 2 | `_maybe_adapt_gallery` | врезка адаптации в unlock-путь (за liveness/anti-screen гейтами) |
| Step 3 | too-dark гейт в unlock-хендлере + поле `scene_luma` в `VerifyOutcome` (helpers `scene_luma`/`evaluate_low_light` — в `lowlight.py`) | честный отказ в темноте, нейтральный к lockout |
| Step 3.3 | `_analyze_burst` (выделен бёрст) + `_maybe_boost` | boost переиспользует ТОТ ЖЕ путь без дублирования кадров |
| Step 4 | `_acquire_camera` | bounded open + reason `camera-busy` |
| Step 5 | `stop_event` / `_wake_accept` (self-connect разблокирует `ConnectNamedPipe`) / `_console_ctrl_handler` (Ctrl+C) / `_drain_until_client_closes` (233-handshake) / watchdog-pause | корректный graceful-shutdown без гонки/233 |

**Локи целы (git-сверка `259d30e..HEAD`):** `verify_frame`, `_prep_cuda_dlls` (GPU-фикс), NULL DACL / pipe-security — НЕ в диффе Этапа 3. Пайп **аддитивен**: шаги надёжности (3–5) добавили НОЛЬ новых команд, только reason-токены `too-dark` / `camera-busy`; команды `shutdown`/`pause_camera`/`resume_camera` уже были на базе Этапа 3 (Этап 2). Локскрин (`credential_provider/`), `installer/`, `presence_monitor/` — не тронуты.

**Новые config-поля (append-only, дефолты):**

| Поле | Дефолт | Смысл |
|-|-|-|
| `low_light_luma_min` | `45.0` | флор scene-luma; ниже → `too-dark` (0 = гейт выкл) |
| `low_light_boost` | `true` | поднять exposure и переснять перед too-dark |
| `low_light_exposure_step` | `2.0` | шаг EV для boost |
| `camera_open_retries` | `2` | доп. попытки open перед вердиктом `camera-busy` |
| `camera_open_timeout_s` | `3.0` | бюджет всего open-retry-loop, сек |
| `watchdog_ping_timeout_s` | `2.0` | бюджет одного пинга (зависший сервер = провал) |
| `watchdog_fail_threshold` | `3` | провалов подряд до рестарта |
| `watchdog_interval_s` | `30.0` | период пинга в self-loop |
| `watchdog_pause_ttl_s` | `300.0` | TTL паузы намеренного стопа (самолечение) |

> Step 1/2 (тоже append-only): `enroll_min_det_score=0.65`, `enroll_min_sharpness=80.0`, `enroll_luma_min=55.0`, `enroll_luma_max=210.0`, `enroll_min_frames=3`; `adaptive_gallery=false`, `adaptive_margin=0.17`, `adaptive_max_size=10`, `adaptive_cooldown_s=1800.0`.
> Порог `threshold` изменён `0.45 → 0.32` в `8c30224` (из измерений), шагами 3–5 не трогался.

**Ре-энролл (6.1):** 19 QC-кадров приняты (1 DROP `bright>210`); живой self-distance mean=0.0855 / max=0.104 (было ~0.2); 3/3 unlock grant с первой попытки (best 0.065–0.073, margin ~0.25, screen 0/5, 200–245 мс); порог `0.32` НЕ менялся.

**Перенесённые TODO (в следующие этапы):**

- (a) Жёсткий cap одиночного `open_fast` под эксклюзивной контенцией → **Этап 5** (вместе с CP-таймаутом).
- (b) Тихий режим watchdog (убрать чёрные окна `schtasks`/CIM) → **Этап 6/7**.
- (c) Читаемость раздувшегося `service.py` → **Этап 6** (НЕ рефакторить перед мержем).
- (d) Repo-wide EOL-нормализация → **Этап 5/6** (из handoff).

**Анти-отравление (напоминание):** adaptive-галерея по умолчанию OFF; включать только при `anti_screen=true`; ceiling якорится на enrollment-базу; при любом сомнении в чистоте — `clear_adaptive()` или ре-энролл (жёсткий откат). Сквозное напоминание про System Protection до Этапа 7 — см. блок вверху файла.

\---

## Этап 4 — харднинг канала/хранения (Stage 4 — channel/storage hardening) — DONE ✅

> Ветка `stage4-hardening` (после `07e0830`; готова к мержу в `master`, ещё **НЕ слита / НЕ запушена**). Дата: 2026-07-09.
> Коммиты: `2b955a8` (Шаг 3: DACL пайпа), `557e9de` (Шаг 4: анти-сквоттинг), `3d72edc` (Шаг 5: SID-гейт `unlock`), `07e0830` (Шаг 6: custody энтропии); `efc8b85` — воспроизводимые selftest'ы Этапа 4.

**Что сделано по шагам (все дефолты — безопасны и прозрачны; поведение по умолчанию не меняется):**

- **Шаг 3 — явный DACL пайпа** (`service.py`, `_build_pipe_sa`): NULL DACL (`SetSecurityDescriptorDacl(1,None,0)` = Everyone) заменён на descriptor из SDDL — **SELF=GA** (владелец/сервер, чтобы per-connection re-create инстанса шёл под токеном SELF), **SYSTEM=GRGW** (локскрин-CP коннектится как SYSTEM), **без Everyone-ACE**, плюс **Medium mandatory-label (NoReadUp/NoWriteUp)** против same-user low-IL процессов. Старое тело сохранено как `_build_sa_everyone_legacy` (откат через `pipe_hardened_sd=false`). Собран через `ConvertStringSecurityDescriptorToSecurityDescriptor` (в pywin32 312 `SetEntriesInAcl` не экспортится → SDDL). Живой descriptor пайпа: `D:(A;;FA;;;<SELF>)(A;;0x12019f;;;SY)S:(ML;;NWNR;;;ME)`. (C1, часть C5.)
- **Шаг 4 — анти-сквоттинг** (`service.py`, `tools/pipe_client.py`): в `openMode` добавлен `FILE_FLAG_FIRST_PIPE_INSTANCE` (литерал `0x00080000` — pywin32 312 его не экспортит) под `pipe_first_instance`. Если имя пайпа уже занято (сквоттер) — `CreateNamedPipe` падает → **loud log + чистый выход** (не тихий краш-луп; watchdog ретраит, каждый отказ залогирован). Безопасно для per-connection re-create: цикл `CloseHandle`-ит прошлый инстанс ДО следующего `CreateNamedPipe`, так что своего инстанса в этот момент нет (доказано: 10 reconnect подряд). Клиент (`pipe_client`, эмуляция Stage-5 CP): перед отправкой проверяет **server-SID ∈ {SELF, SYSTEM}** через `GetNamedPipeServerProcessId` → токен; mismatch → отказ, запрос не шлётся. `presence_monitor`/`watchdog` не трогали (доверенные SELF-циклы). (C2.)
- **Шаг 5 — SID-гейт на `unlock`** (`service.py`): новый `pipe_unlock_require_system` (дефолт **FALSE**). Включённый — требует токен-SID клиента == **SYSTEM (S-1-5-18)**; любой другой (вкл. нечитаемый → `None`) получает `{"ok":false,"reason":"not-authorized"}` **ДО** lockout / verify / `load_password` (пароль и камера не задействованы). Читает SID сервер-сайд `_pipe_client_sid_string` (`GetNamedPipeClientProcessId`→токен, без impersonation — чтение identity не требует привилегий). Гейт **ТОЛЬКО на `unlock`**; прочие команды (`shutdown`/`reset_lockout`/`pause_camera`/`resume_camera`/`reload_config`/`verify`/`status`/`ping`/`presence`/`build_enrollment`) не тронуты — их режет DACL Шага 3, трей-Quit (`shutdown`) как SELF работает. Дефолт FALSE намеренно: реального CP (SYSTEM) в Этапе 4 нет, dev-тест как SELF; **Этап 5 флипнет в True**. (C3, часть C5.)
- **Шаг 6 — custody энтропии** (`credentials.py`): публичный хардкод `ENTROPY=b"face-unlock:v1"` для НОВЫХ шифрований заменён на per-install `os.urandom(32)` в `pipe_entropy.bin` (рождается под protected-DACL `D:P(A;;FA;;;<SELF>)(A;;FA;;;SY)` — без Everyone/наследования, тем же SDDL-механизмом что пайп). Блоб версионируется префиксом `b"v2:"` (v1-DPAPI-блоб начинается с DPAPI-magic, никогда с этого префикса → детект однозначен). **Одноразовая авто-миграция v1→v2** в `load_password`: расшифровать старой энтропией → пере-шифровать per-install секретом → атомарная перезапись (`temp`+`os.replace`). `load_password` **никогда не бросает** — любой сбой → `None`, как при отсутствии блоба (unlock деградирует на ввод пароля; риск лок-аута 0, счётчик к этому моменту уже улажен). DPAPI остаётся user-scope. Константа `ENTROPY` оставлена — нужна для чтения/миграции v1, для новых шифрований не используется. (§2.)

**Threat-model (пересмотр — честно; это ядро этапа):**

Изначальный план (§1, C3/C4; MASTER-TZ §6/§7) требовал nonce + подпись ответа. По ходу Этапа 4 — **сознательный разворот, не пропуск:**

- **Граница канала = OS-контроли, не крипта.** Явный DACL (SELF+SYSTEM, без Everyone) + Medium mandatory-label (режет same-user low-IL) отсекают *другого не-админ юзера* от самого пайпа; `FIRST_PIPE_INSTANCE`+loud-fail закрывают сквоттинг имени; клиент проверяет server-SID; SID-гейт `unlock`→SYSTEM (Этап 5) держит пароль только для локскрин-CP. Всё это — токен-SID проверки, **ноль крипты, ноль новых зависимостей**.
- **nonce/подпись СНЯТЫ как избыточные.** Общий ключ, читаемый обоими концами (SELF-сервис И SYSTEM-CP), *не аутентифицирует* — кто читает ключ, тот и подделает MAC. Чужого не-админ юзера уже режет DACL (он не откроет ни пайп, ни ключ-файл). Same-user malware читает и ключ, И DPAPI-блоб напрямую — подпись не помогает. SYSTEM всемогущ. HMAC добавил бы лишь целостность/анти-реплей, но у одноразового message-пайпа за закрытым DACL реплеить нечего. Асимметрия имела бы смысл только для обратного канала (CP проверяет сервер) — там дешевле бесплатная проверка `GetNamedPipeServerProcessId`-SID (уже сделана), без вендоринга крипто-C++. **Это сознательное отклонение от DoD §6/§7 — предложенный дифф в handoff Этапа 4.**
- **Остаточный риск (ДОМИНИРУЮЩИЙ): same-user disclosure.** DPAPI user-scope блоб расшифровывается тем же юзером **мимо** пайпа/сервиса/лица (прочитал `credentials.bin` + вызвал `CryptUnprotectData`). Фикс энтропии (Шаг 6) — лишь **speed-bump** (файловый секрет вместо публичной константы: «скопируй константу с GitHub» → «прочитай локальный файл в рантайме»), **НЕ фикс**. Настоящий фикс = **SYSTEM-custody выдачи пароля** (расшифровка только в SYSTEM-компоненте после верифицированного матча; у юзер-процесса нет пути к паролю) → **design-item Этапа 5**.
- **TPM отложен:** в pywin32 нет NCrypt/Platform-Crypto-Provider-обёртки (достижимо только через `ctypes`, нетривиально); и без SYSTEM-custody TPM-seal same-user не мешает (тот же юзер всё равно расшифрует). Берём в Этап 5 вместе с custody.
- **Local admin = bypass-by-design** (владение пайпом / SeDebug сервиса / стать SYSTEM) — вне уровня «convenience-grade+»; в модель угроз не берём.
- **Итог границы:** Этап 4 честно защищает от *другого не-админ локального юзера* (и same-user low-IL через mandatory-label). Same-user-под-своей-учёткой и local-admin — вне канала; это **custody-вопрос**, вынесен в Этап 5.

**Локи целы (git-сверка `16a0101..HEAD`):** тронуты 4 файла — `config.py` (3 append-only поля + fail-loud validate), `service.py` (`_build_pipe_sa`+legacy+SID-хелперы, `FIRST_PIPE_INSTANCE`+squatter-refuse, SID-гейт+проброс handle), `tools/pipe_client.py` (server-SID проверка), `credentials.py` (per-install энтропия+миграция). НЕ в диффе (ни в `+`, ни в `−`): `verify_frame`, `_prep_cuda_dlls`, `threshold`/`0.32`, liveness-числа (`HF_THRESH`/`EAR_THRESH`/`SCREEN_DOUBT_FRAC`/`STRONG_MARGIN`), QC/adaptive/low-light/camera/watchdog-константы. `credential_provider/`, `installer/`, `presence_monitor/` — **0 изменений**.

**Новые config-поля Этапа 4 (append-only, дефолты безопасны/прозрачны):**

| Поле | Дефолт | Смысл |
|-|-|-|
| `pipe_hardened_sd` | `true` | явный DACL SELF+SYSTEM + Medium-label вместо legacy NULL DACL (`false` = откат) |
| `pipe_first_instance` | `true` | `FIRST_PIPE_INSTANCE` на сервере + проверка server-SID в `pipe_client` |
| `pipe_unlock_require_system` | `false` | гейт `unlock`→только SYSTEM-caller; **дефолт OFF** (нет CP в Этапе 4; Этап 5 → `true`) |

**Зависимости:** `requirements.lock` == venv `pip freeze` — **ноль новых зависимостей** (актуален, не трогали). Вся крипта — stdlib `hmac`/`hashlib` при необходимости; `pywin32` уже был. Плюс этапа.

**Selftest'ы Этапа 4 (воспроизводимые, camera-free, temp-home):** `tools/pipe_hardening_selftest.py` (descriptor-SDDL, live server/client-SID, unlock SID-гейт), `tools/credentials_selftest.py` (fresh v2 + locked `pipe_entropy.bin`, миграция v1→v2, corrupt/missing→None, DACL SELF+SYSTEM), агрегатор `tools/stage4_selftest.py` (`python -m tools.stage4_selftest`). Прогон на venv 3.12: **STAGE 4 SELFTESTS: ALL OK**.

**Регресс Этапов 2–3 (venv 3.12):** 12 pure selftest'ов зелёные (`adaptive`/`audit`/`camera_busy`/`enroll_qc`/`liveness`/`liveness_verdict`/`lockout`/`lowlight_boost`/`lowlight_gate`/`lowlight_probe`/`threshold_recommend`/`watchdog`) + `shutdown_integration` OK (shutdown+Ctrl+Break без 233 против хардненного сервиса).

**Оставлено Bao на живой прогон:** watchdog-restart (нужен зарегистрированный `FaceUnlock-Watchdog` + kill-then-start; на dev-машине задачи не зарегистрированы) и `camera_busy_integration` (реальная камера). **На реальной машине при первом чтении обновлённым сервисом `~/.face-unlock/credentials.bin`** произойдёт одноразовая миграция v1→v2 + создание `pipe_entropy.bin` — by design (тесты гнались на temp-home, реальный профиль не тронут).

\---

## Этап 5 — Credential Provider (C++) + локскрин — closeout (мерж отложен)

> Ветка `stage5-credential-provider` (после `e527ab6`; **НЕ слита в `master`** — мерж отложен как отдельный шаг closeout'а). Дата: 2026-07-10.
> Коммиты: `58928ce` (Шаг 2: переписан JSON-парсер `PipeClient.cpp` + юнит-тест `tests/test_parser.cpp`), `3d01357` (Шаг 3: клиентская проверка server-SID — **позже переписана**, см. C), `88550cc` (Шаг 4: RDP-off в `SetUsageScenario`, аддитивность + bounded timeout), `38c6cb2` (Шаг 5: README под реальный код), `daee6af` (Шаг 6b: живой `unlock_harness`), `89bcc38` + `f4655c2` (5c: диагностика), `bab8705` (5d: **FIX** серверного SID-резолвера), `e527ab6` (5e: **FIX** клиентской проверки доверия).

**Основная цель этапа достигнута: вход лицом с реального локскрина работает вживую.** Живой прогон 2026-07-10 01:04:07 — `connection accepted (pid=31256 image=LogonUI.exe integrity=? session=? openable=no)` → `request cmd=unlock` → `verify verdict=PASS matches=5/2 best=0.080 margin=0.240 blink=0 screen=0/5 sceneL=76.1 mode=fast`; в `audit.jsonl` тот же момент — `outcome: "granted"`. Гейт `pipe_unlock_require_system` включён в живом конфиге (`~/.face-unlock/config.toml`); дефолт в репо (`config.py`) **не менялся** — `config.py` не в диффе ветки, флип дефолта остаётся отдельным шагом.

**Локи целы (git-сверка `master..HEAD`, 10 файлов):** `credential_provider/*` (парсер + тесты, server-SID, RDP-off, README), `face_service/service.py` (диагностика + SID-резолвер), `tools/pipe_hardening_selftest.py` (адаптирован под импересонацию: клиент теперь пишет сообщение, которое сервер читает ДО импересонации). НЕ в диффе (ни в `+`, ни в `−`): `verify_frame`, `_prep_cuda_dlls`, `threshold`/`0.32`, liveness-числа (`HF_THRESH`/`EAR_THRESH`/`SCREEN_DOUBT_FRAC`/`STRONG_MARGIN`), QC/adaptive/low-light/camera/watchdog-константы. `face_service/config.py`, `credentials.py`, `installer/`, `presence_monitor/` — **0 изменений**.

**B. Санкционированные правки в периметре Этапа 4 — ОБОСНОВАННОЕ ОТКЛОНЕНИЕ:**

Периметр Этапа 4 (`service.py`: пайп-DACL, анти-сквоттинг, SID-гейт `unlock`) был закрыт как DONE. Живой локскрин вскрыл два дефекта, лечимых ТОЛЬКО внутри этого периметра. Обе правки **санкционированы Bao по итогам живого теста** — это отклонение от «периметр заморожен», а не пропуск.

- **Серверный SID-резолвер `unlock`-гейта (`bab8705`).** *Было (Этап 4):* `_pipe_client_sid_string` резолвил SID клиента цепочкой `GetNamedPipeClientProcessId` → `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` → `OpenProcessToken` → `TokenUser`. *Дефект (вскрыт живым тестом):* сервис крутится под **ОГРАНИЧЕННЫМ** интерактивным юзером (Планировщик, `-RunLevel Limited`), а medium-IL стандартный юзер **не может** `OpenProcess` SYSTEM-процесс (LogonUI) без `SeDebugPrivilege` → резолвер возвращал `None` → **боевой SYSTEM-CP отвергался собственным гейтом**. *Стало:* SID клиента резолвится через `win32security.ImpersonateNamedPipeClient(handle)` → `OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, bOpenAsSelf=True)` → `TokenUser` → `RevertToSelf`: пайп-подсистема отдаёт серверу токен клиента напрямую — без `OpenProcess`, мимо DACL-барьера. `RevertToSelf` стоит в `finally` (под `if impersonated`) и отрабатывает на **ВСЕХ** путях выхода — и на `return` внутри `try`, и на `except → return None`; иначе переиспользуемый serve-тред остался бы под токеном клиента и следующий `WriteFile` / коннект пошли бы под ним. Импересонация выполняется в `_handle` **ПОСЛЕ `ReadFile`** (предусловие `ImpersonateNamedPipeClient` — сообщение уже должно быть прочитано), на том же треде.
  ⚠️ `ImpersonateNamedPipeClient` живёт в **`win32security`**, НЕ в `win32pipe` (pywin32 312).
  **ИНВАРИАНТ ГЕЙТА НЕ ОСЛАБЛЕН:** сравнение по-прежнему `sid != SYSTEM_SID_STRING` (`"S-1-5-18"`), user-SID по-прежнему отвергается с `not-authorized` ДО lockout / verify / `load_password`. Сменён **ТОЛЬКО МЕХАНИЗМ** резолва identity — свойство безопасности то же, крипты не добавлено.

- **Расширенная диагностика (`89bcc38`, `f4655c2`).** `89bcc38`: лог отказа `unlock` дополнен полями клиента `pid`/`image`/`integrity`/`session`/`openable`. **Механизмы у полей РАЗНЫЕ — это легко перепутать:** `pid` — `GetNamedPipeClientProcessId` (API именованного канала); `image` — `CreateToolhelp32Snapshot` + `Process32First/NextW` (toolhelp-перечисление, **единственное поле без `OpenProcess`**); `session` — `win32ts.ProcessIdToSessionId`; `integrity` — **именно через** `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `OpenProcessToken(TOKEN_QUERY)` + `GetTokenInformation(TokenIntegrityLevel)`. Пятое поле `openable=<yes|no>` — это и есть результат попытки `OpenProcess`, и оно же главный различитель: боевой CP = `image=LogonUI.exe openable=no`, SELF-харнес = `image=unlock_harness.exe openable=yes`. `f4655c2`: в `_serve_one` добавлены `connection accepted (...)` (сразу после `ConnectNamedPipe`, **ДО** `ReadFile`) и `connection closed before request (winerror=N)` на `BROKEN_PIPE`/`PIPE_NOT_CONNECTED`/`NO_DATA` — без них клиент, отвалившийся на СВОЕЙ pre-write проверке, не оставлял на сервере следа вообще (неотличимо от «не коннектился»). Именно эта пара строк и опознала дефект C.
  **РЕШЕНИЕ ПО ДИАГ-ПОЛЯМ `integrity`/`session`: оставлены КАК ЕСТЬ (осознанно).** Под реальным SYSTEM-клиентом (LogonUI) оба печатают `?` — `OpenProcess` запрещён, а `ProcessIdToSessionId` требует того же `PROCESS_QUERY_LIMITED_INFORMATION`: limited-user не читает IL/сессию SYSTEM-процесса без привилегий. Для НЕ-SYSTEM клиентов резолвятся корректно (харнес: `integrity=Medium session=2 openable=yes`) → диагностическая ценность сохраняется, поля **не подрезаем**. Пара `image=LogonUI.exe` + `openable=no` и так однозначно опознаёт боевой CP. Диагностика не логирует ни пароля, ни тела запроса и не трогает механизм гейта.

**C. Клиентская проверка доверия — ИЗВЕСТНЫЙ ОСТАТОК (trade-off):**

- **`IsTrustedServerSid` переписана (`e527ab6`).** *Дефект:* прежняя версия (`3d01357`) резолвила «юзера сессии» через `WTSQueryUserToken` — на secure desktop внутри LogonUI этот вызов молча не отрабатывает, поэтому CP считал легитимный сервис недоверенным и делал `CloseHandle` пайпа **ДО `WriteFile`**. В логе это ровно та подпись, ради которой добавлен `f4655c2`: 00:47:08 и 00:47:09 — `connection accepted (image=LogonUI.exe … openable=no)` → `connection closed before request (winerror=109)`, и ни одной `request cmd=unlock`. *Стало:* `<wtsapi32.h>` и хелпер `SessionUserSidString` удалены; серверу доверяем, если `serverSid` == SYSTEM (`S-1-5-18`), == own-user, ИЛИ `IsRegularUserSid(serverSid)` — структурная проверка (NT-authority `S-1-5` **И** первая под-власть `== 21`, т.е. `S-1-5-21-…`; `S-1-5-18/19/20`, well-known / logon / capability SID'ы отсекаются). Ни session-API, ни крипты.

- **TRADE-OFF (осознанный остаток):** новая проверка доверяет **ЛЮБОМУ** реальному аккаунту `S-1-5-21` в роли сервера — конкретный интерактивный юзер **НЕ пиннится** (пиннинг требует session-API, который на secure desktop падает).
  **Раскрытия пароля отсюда не следует — но по НЕ той причине, о которой хочется сказать.** Направление важно: при успешном матче пароль **ДЕЙСТВИТЕЛЬНО передаётся в ответе пайпа** — `_handle` возвращает `{"ok":true,"username":…,"password":…,"domain":…}`, ответ сериализуется `json.dumps` и уходит `WriteFile`; CP вынимает его в `ParseUnlockResponse` и пакует в `KERB_INTERACTIVE_UNLOCK_LOGON.Password` (более того, CP **отвергает** `ok=true` без непустого пароля). SYSTEM-custody не сделан — пароль по-прежнему ходит по каналу. Подмена **СЕРВЕРА** тем не менее кредов атакующему не даёт, потому что: **(1)** клиент не отправляет никаких секретов — запрос это фиксированный литерал `{"cmd":"unlock"}`, перехватывать со стороны клиента нечего; **(2)** выдать валидный пароль способен только настоящий сервис, расшифровав своё DPAPI-хранилище (user-scope + `pipe_entropy.bin` под PROTECTED-DACL `SELF+SYSTEM`), которое сквоттер прочитать не может. Максимум сквоттера — вернуть заведомо неверные креды → отказ логона → **DoS с откатом на плитку пароля/PIN**.
  **Честно про анти-сквоттинг (не переоценивать).** DACL пайпа управляет **уже существующим объектом**, а не резервирует ИМЯ в неймспейсе. Бит-в-бит: маска SYSTEM `0x12019f` (= `GRGW`; `FILE_GENERIC_WRITE` тянет `FILE_APPEND_DATA=0x0004`, который на пайпе и есть `FILE_CREATE_PIPE_INSTANCE`) право создания инстанса **содержит**, у SELF `FA` — тоже, Everyone-ACE нет. Значит для **живого** пайпа добавить инстанс могут только SELF+SYSTEM — это верно. **НО** пока сервис не поднят (до логона, после kill/краша) имя свободно, и любой локальный `S-1-5-21`-юзер занимает его своим `CreateNamedPipe` со своим дескриптором. `FILE_FLAG_FIRST_PIPE_INSTANCE` сквоттинг **не предотвращает** — он лишь заставляет НАШ сервис отказаться стартовать (детект + loud log). А раз клиентское правило доверяет любому `S-1-5-21`, сквоттер, выигравший гонку за имя в «холодном окне», будет CP признан доверенным.
  **Итог остатка:** не «ТОЛЬКО DoS при гарантированном анти-сквоттинге», а **DoS + интерпозиция в холодном окне** (выигрыш атакующего ограничен: вернуть он может лишь известные ему креды). Приемлемо для уровня «convenience-grade+»; настоящее закрытие — **SYSTEM-custody выдачи пароля** (design-item, перенесён из Этапа 4 и в Этапе 5 **НЕ реализован**). Зафиксировано как осознанный остаток.

**Известные остатки в КОДЕ (в этом коммите не правились — задача doc-only):**

- Докстринг `_pipe_client_diag` (`service.py`) и сообщение коммита `89bcc38` утверждают, что «image + session читаются без `OpenProcess` и потому резолвятся даже для SYSTEM-клиента». Живой лог опровергает это для `session`: у всех строк LogonUI стоит `session=?`, потому что `ProcessIdToSessionId` требует того же `PROCESS_QUERY_LIMITED_INFORMATION`. Реально резолвится только `image`. → правка комментария, Этап 6.
- `wtsapi32` остался в линковке (`credential_provider/CMakeLists.txt`, `credential_provider/tests/CMakeLists.txt`) после удаления `WTSQueryUserToken` в `e527ab6` — мёртвая запись. → чистка, Этап 6/7.
- Комментарий-шапка в `PipeClient.cpp` («a foreign account cannot own this pipe») наследует ту же переоценку анти-сквоттинга, что разобрана выше. → правка комментария, Этап 6.

**Перенесено дальше:** SYSTEM-custody выдачи пароля (доминирующий same-user риск, из Этапа 4) — **НЕ сделан**; опц. TPM-seal — **НЕ сделан**. Мерж ветки, флип дефолта `pipe_unlock_require_system` в репо и перевод статуса проекта — **отдельные шаги closeout'а**, в этот коммит не входят.
