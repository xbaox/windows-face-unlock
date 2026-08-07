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
* **Этап 4 (DONE ✅):** харднинг канала — явный DACL `SELF+SYSTEM` + Medium mandatory-label (C1), `FIRST_PIPE_INSTANCE` + проверка server-SID клиентом (C2), SID-гейт `unlock`→SYSTEM (C3/C5; дефолт OFF в Этапе 4, флипнут в `true` на closeout'е Этапа 5), per-install энтропия + авто-миграция v1→v2 (§2). **nonce/подпись (C3,C4) СНЯТЫ** как избыточные к OS-границе (обоснование — секция «Этап 4 … Threat-model (пересмотр)»). DoS-команды (C5) режет DACL. Остаток → Этап 5.
* **Этап 5:** Credential Provider (C++, VS2022+CMake), свой GUID, защищённый протокол, таймаут, фолбэк, RDP-off, регистрация. ⚠️ ЛОКСКРИН. **Перенесено из Этапа 4:** SYSTEM-custody выдачи пароля (доминирующий same-user риск), флип `pipe_unlock_require_system=true`, опц. TPM-seal.
* **Этап 6 (DONE ✅):** UX (визард, трей, RU/EN) + блок 7 (харднинг автостарта и локскрина). ⚠️ Визард и трей — на **tkinter**, не на PySide6: унаследованный из базы форка GUI молча перекрыл план §3 — **отклонение от DoD/MASTER §3**, разбор в секции «Этап 6 … A».
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

## Этап 5 — Credential Provider (C++) + локскрин — DONE ✅

> Ветка `stage5-credential-provider` — **слита в `master` fast-forward** на closeout'е 2026-07-17. Дата живого прогона: 2026-07-10.
> Коммиты: `58928ce` (Шаг 2: переписан JSON-парсер `PipeClient.cpp` + юнит-тест `tests/test_parser.cpp`), `3d01357` (Шаг 3: клиентская проверка server-SID — **позже переписана**, см. C), `88550cc` (Шаг 4: RDP-off в `SetUsageScenario`, аддитивность + bounded timeout), `38c6cb2` (Шаг 5: README под реальный код), `daee6af` (Шаг 6b: живой `unlock_harness`), `89bcc38` + `f4655c2` (5c: диагностика), `bab8705` (5d: **FIX** серверного SID-резолвера), `e527ab6` (5e: **FIX** клиентской проверки доверия).

**Основная цель этапа достигнута: вход лицом с реального локскрина работает вживую.** Живой прогон 2026-07-10 01:04:07 — `connection accepted (pid=31256 image=LogonUI.exe integrity=? session=? openable=no)` → `request cmd=unlock` → `verify verdict=PASS matches=5/2 best=0.080 margin=0.240 blink=0 screen=0/5 sceneL=76.1 mode=fast`; в `audit.jsonl` тот же момент — `outcome: "granted"`. Гейт `pipe_unlock_require_system` на момент прогона был включён через живой конфиг (`~/.face-unlock/config.toml`); на closeout'е дефолт в репо флипнут `False`→`True` — единственная правка `config.py` (см. финал раздела).

**Локи целы (git-сверка `master..HEAD`, 10 файлов этапа + closeout-флип в `config.py`):** `credential_provider/*` (парсер + тесты, server-SID, RDP-off, README), `face_service/service.py` (диагностика + SID-резолвер), `tools/pipe_hardening_selftest.py` (адаптирован под импересонацию: клиент теперь пишет сообщение, которое сервер читает ДО импересонации). НЕ в диффе (ни в `+`, ни в `−`): `verify_frame`, `_prep_cuda_dlls`, `threshold`/`0.32`, liveness-числа (`HF_THRESH`/`EAR_THRESH`/`SCREEN_DOUBT_FRAC`/`STRONG_MARGIN`), QC/adaptive/low-light/camera/watchdog-константы. `face_service/config.py` — ровно **одна** правка (closeout-флип дефолта `pipe_unlock_require_system` `False`→`True`); `credentials.py`, `installer/`, `presence_monitor/` — **0 изменений**.

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

**Остатки в КОДЕ, зафиксированные на closeout'е Этапа 5 — ВСЕ ТРИ ЗАКРЫТЫ в `ae376c5` (Этап 6, блок 7-A2):**

- Докстринг `_pipe_client_diag` (`service.py`) и сообщение коммита `89bcc38` утверждали, что «image + session читаются без `OpenProcess` и потому резолвятся даже для SYSTEM-клиента». Живой лог опроверг это для `session`: у всех строк LogonUI стоит `session=?`, потому что `ProcessIdToSessionId` требует того же `PROCESS_QUERY_LIMITED_INFORMATION`. Реально резолвится только `image`. → **исправлено в `ae376c5`.**
- `wtsapi32` оставался в линковке (`credential_provider/CMakeLists.txt`, `credential_provider/tests/CMakeLists.txt`) после удаления `WTSQueryUserToken` в `e527ab6` — мёртвая запись. → **снят в `ae376c5`** (в дереве не осталось ни одного символа `WTS*`).
- Комментарий-шапка в `PipeClient.cpp` («so a foreign/service account cannot own this pipe») наследовал ту же переоценку анти-сквоттинга, что разобрана выше. → **исправлен в `ae376c5`**, вместе с тем же утверждением во встроенном комментарии `IsTrustedServerSid` (одна переоценка на двух участках файла).

**Closeout выполнен (2026-07-17):** дефолт `pipe_unlock_require_system` флипнут `False`→`True` в `config.py` (до этого гейт включался только через живой конфиг `~/.face-unlock/config.toml`), статус Этапа 5 → **DONE** (здесь и в MASTER-TZ), ветка влита в `master` (**fast-forward**). **НЕ сделано (осознанно перенесено):** SYSTEM-custody выдачи пароля (доминирующий same-user риск, из Этапа 4) и опц. TPM-seal — **design-items, Этап 6/7/8**.

\---

## Этап 6 — UX (визард, трей, RU/EN) + Блок 7 (харднинг автостарта и локскрина) — DONE ✅

> Ветка `stage6-ux` — **20 рабочих коммитов Этапа 6 (`8b7c68a..679c883`) + closeout-коммиты сверху; влито в `master` --no-ff, merge `b3ead4a`.** Дата closeout: 2026-07-20.
> Блоки 1–4 (UX-ядро): `60aa921`, `c27a79f`, `7932eb3`, `1736efa`, `1474e64`, `da72353`, `314eef0`, `a72c694`.
> Блок 5 (визард ре-энролла): `081bc8b`, `bcc8b0e`, `e084e1d`, `3bcbc7a`, `a2940df`.
> Блок 6 (камерный путь сервиса + латентность): `fe000e9`, `bc4e25b`, `4267eba`.
> Блок 7 (харднинг): `80b562f` (A1), `ae376c5` (A2), `b8e1037` (A3), `679c883` (A4).

**Что сделано в блоках 1–6 (сжато):** `liveness_mode` выведен в Настройки; полная RU-локализация (142/142) с вычисткой унаследованных DeepFace-строк; секции Настроек под кноби Этапов 2–4; событийные уведомления (`notify_*`) + персистентный тумблер авто-лока; детерминированный teardown tk (биндинг `master=` у Var, pre-destroy nulling, `ImageTk.PhotoImage` строится только в mainloop) — лечение multi-interpreter Tcl-нестабильности; в визарде — QC-коучинг кадров, честный прогресс-гейт и реальная причина провала Build; камерный путь сервиса (таймаут-пропсы, swap-then-release, `release` в `finally`); посегментный зонд холодной латентности.

**A. Тулкит GUI: tkinter вместо PySide6 — ОТКЛОНЕНИЕ от DoD/MASTER §3**

MASTER §3 предписывает **PySide6**; визард и трей живут на **tkinter/ttk**. Фиксируем честно: **это отклонение от DoD/MASTER §3, и оно НЕ является сознательным разворотом в стиле Этапа 4.**

- **Причины разворота в репо НЕТ.** Сплошной поиск (все трекаемые файлы, все сообщения коммитов на всех ссылках, все доки рабочего дерева) не дал **ни одного** места, где tkinter сопоставлялся бы с PySide6/Qt. Ни коммит, ни комментарий, ни докстринг, ни README/INSTALL/KNOWN_ISSUES не объясняют выбор. **Обоснование здесь не выдумывается** — его нечего цитировать.
- **Что произошло на самом деле:** GUI пришёл из **базы форка** — `dcaa766` от 2026-04-21, автор `Admin`, за ~3 месяца до Этапа 0 (`b4d96fc`, 2026-07-07). Этап 6 был отскоуплен как инкрементальная доводка уже существующего кода: он визард **правил**, но не **создавал**. План «PySide6» никто явно не отменял — он был **молча перекрыт унаследованным кодом**.
- **Текущее состояние (констатация факта, НЕ доказательство решения):** Qt в проекте отсутствует физически — PySide6 нет ни в `requirements.txt`, ни в `requirements.lock`; в `installer/windows_face_unlock.spec:62` он числится среди исключений бандла. ⚠️ Последнее **нельзя** предъявлять как аргумент выбора тулкита: комментарий над списком (`spec:61`) объясняет исключения размером бандла и пакетами, которые тянул DeepFace, — к решению по GUI он отношения не имеет.
- **Честный остаток:** цена tkinter уже оплачена живой болью — `3bcbc7a` (multi-interpreter Tcl instability, детерминированный teardown). ⚠️ Этот коммит **не** является аргументом «за» tkinter: он лечит дефект, порождённый multi-root-моделью самого tkinter, и о Qt не говорит ничего.
- Roadmap-строка в этом же файле (§«Карта») до сих пор обещала «визард PySide6» — **правится в этом же коммите**.

**B. Блок 7 / A1 — харднинг автостарта (`80b562f`)**

- **`ExecutionTimeLimit` = `PT0S` во всех ЧЕТЫРЁХ регистраторах** (`setup.ps1:43`, `installer/postinstall/register_tasks.ps1:21`, `tools/register_tasks.ps1:13`, `tools/register_watchdog_task.ps1:13`). Четыре — это **полный** набор: репо-wide поиск `Register-ScheduledTask`/`New-ScheduledTaskSettingsSet` даёт хиты только там (`installer.iss` лишь дёргает `schtasks /Run|/End|/Delete`, задач не создаёт). **Было — дефолт Планировщика `PT72H`:** в дереве до коммита слово `ExecutionTimeLimit` не встречалось ни разу, все четыре settings-set обрывались на `-Hidden`. Практический смысл: persistent-сервис **умирал через 3 дня** аптайма.
- **Death-wait вместо слепого sleep в ЧЕТЫРЁХ рестарт-путях:** `tools/watchdog.py:133-147` (`_wait_dead()`, вызов на `:156`, вместо `time.sleep(1.0)`), `tools/register_tasks.ps1:35-42` (вместо `Start-Sleep 1`), `tools/restart_service.ps1:25-32` и `tools/clean_restart.ps1:21-28` (вместо `Start-Sleep 2`). Ожидание **ограничено** (`_DEATH_WAIT_S = 10.0`, опрос `_DEATH_POLL_MS = 200`) и никогда не висит. ⚠️ **Точность:** подтверждающий лог `all killed processes confirmed gone` есть **в 1 из 4** путей (`watchdog.py:147`); три `.ps1` на успехе молчат и выводят только `Write-Warning` по таймауту.
- **`_MATCH_PS` (`tools/watchdog.py:47-50`)** — единый критерий поиска процессов **ТОЛЬКО внутри `tools/watchdog.py`** (три потребителя: `_KILL_PS`, `_COUNT_PS`, `_WAIT_DEAD_PS`). ⚠️ **Не** репо-wide унификация: три `.ps1` держат собственные `$matching`-блоки с **намеренно другим** критерием (`python.exe` OR `pythonw.exe`, `face_service` OR `presence_monitor`, тогда как `_MATCH_PS` — только `pythonw.exe` + `face_service`). Итого пять определений, а не один общий хелпер.
- **`print()` → файловый лог:** `tools/watchdog.py` переведён на `logging` (11 мест), обработчики — `FileHandler(~/.face-unlock/watchdog.log)` + `StreamHandler`.
- ⚠️ **ЧЕСТНО — чего НЕ было.** По ходу разбора возникла версия, что старый `restart_service.ps1` оставлял **воркер-сироту, державший mutex**. **Это неверно, версия снята чтением `80b562f^`.** Проход 1 (`:4-11`) на PS 5.1 наполовину мёртв — свойства `$_.CommandLine` у `Get-Process` не существует, остаётся тест по `$_.Path`, ловящий лишь venv-стабы. Но проход 2 (`:13-16`) — WMI-запрос по `CommandLine` — выполнялся **безусловно** и убивал настоящий воркер всегда. **Утечки не существовало; mutex всегда освобождался.** Реальная ценность правки = **схлопывание двух проходов в один критерий + death-wait**, и только это. (Сообщение самого коммита формулирует верно — ошибочной была надстроенная поверх него интерпретация.)

**C. Блок 7 / A2 — мёртвая линковка и переоценивающие комментарии (`ae376c5`)**

- **`wtsapi32` снят из двух CMakeLists** (`credential_provider/CMakeLists.txt`, `credential_provider/tests/CMakeLists.txt`). Запись стала мёртвой после удаления `WTSQueryUserToken` в `e527ab6` (Этап 5, дефект C): в дереве не осталось **ни одного** символа `WTS*`.
- **Исправлены переоценивающие утверждения — ДВА утверждения на ТРЁХ участках** (в подзаголовке коммита сказано «two comments», что недосчитывает участки): шапка `credential_provider/PipeClient.cpp:273-298` и встроенный комментарий в `IsTrustedServerSid` (`:367-368`) несли одну и ту же переоценку анти-сквоттинга; докстринг `_pipe_client_diag` (`face_service/service.py:183`) утверждал, что `image`+`session` читаются без `OpenProcess` — живой лог опроверг это для `session`.
- ⚠️ **Точная цитата:** прежняя шапка гласила «so a **foreign/service** account cannot own this pipe» — со слэшем; и самоцитата в новом комментарии, и запись в этом файле слэш роняли.
- Правка **doc-only** за вычетом удаления мёртвых `.lib`-записей: логика, сигнатуры и числа не тронуты.

**D. Блок 7 / A3 — lockout-тост в залоченной сессии (`b8e1037`)**

- **Предикат `session_locked()`** (`presence_monitor/remote_session.py:87-126`): `OpenInputDesktop` → `GetUserObjectInformation`, имя `Default` = **разлочено**.
- **Ловушка релиза хэндла (реальная).** `CloseDesktop()` стоит в `finally` (`:120-125`) под `if hdesk is not None`. Живая интроспекция pywin32 312: возвращаемый объект — `PyHDESK` с методами `['CloseDesktop','Detach','EnumDesktopWindows','SetThreadDesktop','SwitchDesktop']`; **`.Close()` у него НЕТ** (`PyHDESK` не наследует `PyHANDLE`). Вызов `.Close()` кидал бы `AttributeError` на **каждом** полле, а голый `except Exception: pass` его бы проглотил → **тихая утечка desktop-хэндла на каждый тик**. Использован единственный корректный вызов.
- **Фейл-сейф:** `ERROR_ACCESS_DENIED` → **залочено** (отказ и есть сигнал); любая другая win32-ошибка, не-win32 исключение и отсутствие pywin32 → **разлочено = статус-кво** (тост всё равно сработает). Три независимых маршрута деградации ведут в `False` — предикат не может «съесть» уведомление молча.
- **Гейт ТОЛЬКО на lockout-блоке.** В `presence_monitor/monitor.py` ровно две точки уведомления: `:170-173` (`notify_service_state`) — **не гейчена**, и `:181-184` (`notify_lockout`) — гейчена условием `:180`. `session_locked` встречается в модуле **один раз** (`:180`); прочие продюсеры тостов (`enroll_gui.py:746/752`, `tray.py:189/195/201`) не тронуты.
- **«Replay» — это НЕ очередь, а осознанный не-латч.** Withheld-тост никогда не конструируется: флаг `_lockout_notified` не защёлкивается, и на первом же полле после разлочки то же условие переоценивается заново, а текст собирается из **текущего** `remaining_s`. То есть замена показывает актуальные секунды, а не протухшие.
- ⚠️ **Остаток 1 — имя шире кода.** Предикат буквально проверяет «имя input-десктопа ≠ `Default`», что истинно и для **UAC-секьюр-десктопа**, и для **скринсейвера**. Для этой задачи поведение верное (баллон в любом из трёх состояний не виден), но имя функции и формулировка «залочен» утверждают больше, чем проверяет код. Докстринг (`:88-89`) честен.
- ⚠️ **Остаток 2 — формулировка «эпизод ВСЕГДА начинается в локе» неточна.** Страйки пишутся в единственном месте (`face_service/service.py:823-824`) внутри `unlock`-хендлера, чей SYSTEM-гейт **конфигурируем** (`cfg.pipe_unlock_require_system`, дефолт `True` — `config.py:150`, строка устарела → актуальное в «Этап 7 / 7c»). Верно «нормально», а не «всегда»; сам код от абсолюта не зависит — кейс «эпизод начат в разлоченной сессии» покрыт селфтестом и корректно обработан на `monitor.py:180`.
- **Принято осознанно (end-to-end не догнан).** Юнит-покрытие: `tools/lockout_notify_selftest.py` — три теста, **19 проверок** (не «20/20»): харнес печатает по строке на проверку и на провале — только счётчик провалов; **сводку вида `N/N` он не эмитит вообще**, так что такой цифры и не могло быть. `tools/session_lock_probe.py` — диагностический зонд без ассертов; на реальном локскрине подтвердил обе ветки предиката. **Ветка «тост в локе» вживую не догнана** (упёрлись в usage-лимит) и в репо ничем не тестируется: селфтест **стабит** `session_locked`. Логика валидна юнитом и живым зондом; сквозной прогон — долг.
- ⚠️ **Принятое поведение:** если lockout-эпизод **истёк, пока сессия залочена**, тоста не будет вовсе. Это правильно (баллон о давно прошедшем лок-ауте — шум), форензику держит `audit.jsonl`.

**E. Блок 7 / A4 — тест-долг класса `bc4e25b` (`679c883`)**

Долг того же класса, что закрыт в `bc4e25b` (`camera_busy_selftest`): SID-гейт `unlock` режет **handle-less SELF-харнес**.

- **Механизм:** харнес без реального пайп-хэндла → SID не резолвится → `{"ok":false,"reason":"not-authorized"}` **до** записи аудита → пустой audit-список → `IndexError` в тесте.
- **Фикс:** фабрика конфига `_svc_cfg` с `pipe_unlock_require_system=False`. Имя выбрано так, чтобы **не затенять** уже существующий `_cfg` в тех же модулях.
- ⚠️ **ДОКАЗАНО, что это НЕ регресс блока 7** — археология (де-конфлитнутая; две истории, которые легко перепутать):
  1. **Долг SID-гейт → `IndexError` рождён В `5d28508`** — коммите, флипнувшем дефолт гейта в `True`. На `4e91d7a` SID-гейта **ещё не существовало**: он введён в `3d72edc` (Этап 4, Шаг 5). Дистанция флипа — **22 коммита** (`git rev-list --count 4e91d7a..5d28508`), не 23.
  2. **Отдельная история про boost:** `4e91d7a` **создал** `tools/lowlight_boost_selftest.py` — и **со стабом** boost'а. Претензия «boost пришёл, харнес не обновили» верна **только** для `tools/lowlight_gate_selftest.py`.
- **Стаб `_maybe_boost` = defence-in-depth В ТОМ ЖЕ коммите, а не закрытие предсуществующей камерной дыры.** До A4 гейт возвращал управление **раньше**, чем boost-ветка вообще вычислялась, — путь был недостижим. A4 своим же гейт-оффом путь открыл и тут же закрыл. ⚠️ И даже без стаба до реальной камеры оставалось дальше, чем звучит: нужны **ДВА** атрибута (`_cam_lock` **и** `_cam`), а при `persistent_camera=True` (`config.py:52` — строка устарела → актуальное в «Этап 7 / 7c») реальный конструктор `Camera()` всё равно недостижим.
- **Прод не тронут:** в диффе только два файла селфтестов.

**F. ⭐ Ревью-гейт как сила процесса**

Отдельной строкой — потому что это повторяющийся, а не разовый эффект. За окно **систематически ловились собственные додуманные обоснования**, и каждый раз — проверкой чтением/прогоном **ДО** показа диффа:

| Что было додумано | Чем опровергнуто |
|-|-|
| A1: «воркер-сирота с mutex» в `restart_service.ps1` | чтение `80b562f^` — второй WMI-проход убивал воркер всегда; утечки не было |
| A2: переоценка анти-сквоттинга («чужой аккаунт не может владеть пайпом») | разбор холодного окна: DACL правит объектом, а не резервирует имя |
| A4: придаточная про `tools/pipe_hardening_selftest.py:155` | чтение файла — к A4 отношения не имеет |

Плюс на closeout'е тем же гейтом пойманы: «20/20» вместо 19 проверок, «23 коммита» вместо 22, «4 места» путь-дрейфа вместо 6 файлов, «мёртвый хэндл» вместо живого-но-чёрного. ⚠️ И ещё один — уже на самой правке: первичная сверка дала «10 строк» дрейфа, пересчёт по файлам показал, что агрегат вообще некорректен как метрика (зависит от способа счёта) → в §J оставлен поимённый перечень без суммы. **Вывод:** дешевле всего ложное утверждение стоит до коммита; проверка первоисточника (`git show`, чтение файла) обязана предшествовать формулировке, а не следовать за ней.

**G. Итоги живых прогонов**

- **Reboot #1** (холодный autostart, 4 процесса, **без** watchdog в топологии): `verify match:true distance 0.074`; Win+L → `unlock granted distance 0.0587 margin 0.2613 screen 0/5 latency 219ms`. Подтвердило, что LogonUI подхватил пересобранную DLL без `wtsapi32` — путь локскрина цел.
- **Финальный ребут** (холодный autostart, 6 процессов, **с** watchdog): все 6 стартовали одной секундой (20:47) — без mutex-loser'ов, дублей и краш-лупа → **гипотеза «watchdog-гонка на прогреве» закрыта**. `verify match:true distance 0.161`; Win+L → `unlock granted distance 0.1929 margin 0.1271 screen 0/5 latency 187ms` (маржа выше `STRONG_MARGIN` 0.10).
- **Watchdog live-test W3:** kill Service-пары → `ping failed 1/3 → 2/3 → 3/3` → death-wait `all killed processes confirmed gone` (~0.4 с — **измерение, не слепой sleep**) → рестарт → свежие PID → `verify match:true distance 0.037`. Death-wait и пере-захват пайпа (`FIRST_PIPE_INSTANCE`) доказаны боем **без** правок периметра Этапа 4.
- **`PT0S` на живых задачах:** `Get-ScheduledTask` показывает `ExecutionTimeLimit=PT0S` на Service и Presence.
- **Регресс:** селфтесты Этапов 2–4 на venv 3.12 — **14/14**. Трей и Настройки RU без кракозябр, `liveness_mode` виден и переключаем. `fast`↔`paranoid` живые: в `paranoid` при `distance 0.086/0.100`, `real:true` — `match:false` (жест требуется всегда → `NEEDS_GESTURE`); в `fast` при тех же дистанциях — `match:true`. tk-энролл стабилен после полного сброса. Walk-away сработал вживую (полный экран, лицо вбок → 2 страйка × 60 с → залочился).
- ⚠️ **Тайминг `-AtLogOn` — зафиксировать как поведение, не как баг.** Первый вход после **ЛЮБОГО** ребута идёт через **PIN**: сервис зарегистрирован на `-AtLogOn`, то есть стартует **после** входа, поэтому плитка лица упирается в CP-таймаут `kUnlockTimeoutMs = 12000` (`credential_provider/PipeClient.cpp:477`) и честно откатывается на PIN. **By design; кандидат в пользовательские доки** (иначе читается как поломка).

**H. ⭐ Решение по дефолту `adaptive_gallery`: остаётся `False` (live-only)**

Осознанное решение **не флипать** дефолт (`config.py:89`) в `True`.

- **За флип:** adaptive-стор осмыслен, и у Bao **широкая** база (30×512, два условия света).
- **Против (перевешивает):** дефолт бьёт по **свежим** установкам с узкой базой, где потолок `threshold − adaptive_margin` = `0.32 − 0.17` = **0.15** (`recognizer.py:403`) практически недостижим → adaptive становится **no-op**, а поверхность отравления открывается зря. Дефолт-офф безопаснее.
- **УСИЛЕНО живым фактом:** даже широкая база Bao **вечером** даёт `distance 0.16–0.19` > 0.15 → adaptive **и там** no-op. Довод «у меня-то база широкая» эмпирически не спасает.
- Включать — только осознанно, в живом конфиге.

**I. Дрейф-инцидент — механика и лечение (текущие числа)**

- **Механика:** `margin = threshold − best_distance` (`face_service/service.py:428`); при `margin < STRONG_MARGIN` (0.10, `liveness.py:394`) вердикт уходит в «сомнение» (`liveness.py:451`). Отсюда **эффективный порог чистого матча ≈ 0.22** (`0.32 − 0.10`), а не номинальные 0.32.
- **Лечение:** широкий ре-энролл (два условия света, 30 векторов) + adaptive.
- ⚠️ **ОБЯЗАТЕЛЬНЫЙ ПОРЯДОК: adaptive включать ПОСЛЕ финального Build.** `recognizer.py:280` делает `self._adaptive.clear()` на пересборке — включённый раньше adaptive будет стёрт Build'ом.
- **Текущее состояние галереи:** 30×512, два условия света, дрейф-фикс, цела. `adaptive.npz` **отсутствует — by design**, а не баг: Build стирает стор, копится он только на unlock с `distance ≤ 0.15`, а вечерний свет (0.16–0.19) выше потолка. В хорошем свете файл пересоздастся сам.

**J. Путь-дрейф CP-DLL → Этап 7**

Боевая DLL грузится из **`build-cp\Release`** (сверено `reg query` по CLSID `{8414D7B6-…}`), а репо ссылается на `credential_provider\build\` — **6 файлов** (не 4; `README.md` в корне в число хитов **не** входит). Точные места:

| Файл | Строки | Тип |
|-|-|-|
| `installer/build.py` | `:55`, `:59`, `:61`, `:66` | ⚠️ **исполняемый код** |
| `.github/workflows/release.yml` | `:74`, `:75` | ⚠️ **CI** |
| `INSTALL.md` | `:88`, `:89`, `:207` | док |
| `credential_provider/README.md` | `:19`, `:20`, `:23` | док |
| `credential_provider/register.ps1` | `:6` | скрипт |
| `installer/README.md` | `:53` | док |

> Суммарная «цифра строк» здесь намеренно не приводится: она зависит от того, считать ли литерал `"build"` в аргументах `cmake` отдельно от конструкции пути. Перечень выше однозначен, агрегат — нет.

Формулировка сильнее «доки устарели»: **доки врут И `installer/build.py` бьёт по пайплайну сборки** (он хардкодит `CP_DIR / "build"` и достаёт DLL из `build/Release/`), а CI-шаг собирает в тот же несуществующий путь под `continue-on-error: true`, то есть **провал был бы тихим**. Точечная правка одного `register.ps1` проблему **не** решает — нужна консолидация имени каталога. → **Этап 7.**

⚠️ **`build-tests/` — НЕ дрейф:** это задокументированное имя (`credential_provider/tests/CMakeLists.txt:6-7`, `unlock_harness.cpp:21-22`); оно просто не было в `.gitignore`.

**`register.ps1` — отдельный долг Этапа 7 (тихий ложный успех):**
- дефолт `$DllPath = "$PSScriptRoot\build\Release\..."` (`:6`) → на этой машине `Test-Path` не проходит → `exit 1` (`:9-11`);
- проверки elevation нет (только комментарий `# Must be run as Administrator.` на `:2`);
- **возврат `regsvr32` не проверяется** (`:15`/`:18`), а `Write-Host "Registered ..."` печатается безусловно → **провал выглядит успехом, код возврата 0**.
- ⚠️ Наивный фикс не сработает: `regsvr32.exe` — GUI-subsystem-бинарь, PowerShell его **не ждёт**, поэтому проверка `$LASTEXITCODE` бесполезна; корректно только `Start-Process … -Wait -PassThru` с разбором `.ExitCode`.

**K. Принятые микро-долги и поведение → Этап 7**

- **Зомби-камера визарда.** Характеризуется строго по `KNOWN_ISSUES.md` §1 (`e084e1d`): камерный daemon-тред визарда остаётся висеть внутри `cv2.VideoCapture.read()`, его `finally: cap.release()` не выполняется, и **живой** `VideoCapture`-хэндл продолжает держать девайс **в процессе `presence_monitor`**; последующие `verify` открывают камеру «успешно», но читают **чёрные/негодные кадры** (`distance=1.0`, scene luma ≈ 0), а при `persistent_camera=True` чёрная capture **кэшируется и залипает**. ⚠️ **Формулировка «мёртвый хэндл» неверна** и противоречит `e084e1d` — хэндл живой, негодны кадры. Обход (оттуда же): перезапуск процесса `presence_monitor` (или полный сброс); перезапуск **только** сервиса не помогает — хэндл держит другой процесс.
  ⚠️ **РАСХОЖДЕНИЕ, которое не замазываем.** Сегодняшний инцидент (два unlock `NOT_LIVE`, `distance 1.0`, латентность ~7200 мс) дал **ту же сигнатуру**, но **визард не открывался**, а `presence_monitor` **не содержит камерного кода вне `enroll_gui.py`** (presence ходит в сервис по пайпу — `monitor.py:226`). Значит механизм §1 этот случай **не объясняет**: набор триггеров шире, чем описано. Кандидат для живой диагностики — кэш `self._cam` в самом сервисе (`service.py:317` возвращает закэшированный хэндл **без проверки здоровья**), но причина **не установлена** и здесь не утверждается. Следствие для планирования: **процессный вынос визарда сам по себе этот случай не закрыл бы** → фикс Этапа 7 = вынос визарда **+ координация владения камерой**.
  ⚠️ **Обход выше — тоже не универсален.** Он выписан под механизм §1: хэндл живёт в `presence_monitor`, поэтому там перезапуск **только сервиса** бесполезен. Для сегодняшнего класса минимальный обход **другой**: если дело в кэше сервиса, его чистит как раз перезапуск **сервиса** (или полный сброс) — сегодняшний ребут это и сделал. Так что «перезапуск только сервиса не помогает» **нельзя** читать как универсальное правило. Причина не установлена, поэтому точный минимальный обход — **тоже пункт диагностики Этапа 7**.
- **`camera_open_timeout_s` — не дедлайн.** ⚠️ Механизм не тот, что кажется: это **настоящий бюджетный аргумент** (`service.py:320-326` → `camera_open.py:35`), а не «пропса-подсказка» (пропсой является отдельная хардкод-константа `camera.py:14 CAMERA_READ_TIMEOUT_MS`). Дефект в другом: бюджет сверяется **только между попытками** и не проверяется перед последней, а одиночный блокирующий `open_fast` не прерывается — при `camera_open_retries=2` это до трёх неограниченных открытий, и суммарное время может превысить `camera_open_timeout_s` без предела. Комментарии `camera_open.py:22-23`, `config.py:122` (строка устарела → актуальное в «Этап 7 / 7c») и `service.py:312` («never hangs») **переобещают** → правка формулировок + реальный дедлайн, Этап 7. Сюда же — TODO (a) Этапа 3 (cap одиночного `open_fast`).
- **Лизинг камеры:** `CAMERA_LEASE_S = 300` без продления — долгий энролл может пережить собственный lease. → Этап 7.
- **A3-остатки:** «эпизод истёк, пока залочено → тоста нет» (принято, форензику держит audit); ⚠️ **на этой машине единственный рабочий сигнал лока — `access-denied`**, ветка «имя десктопа = `Winlogon`» у medium-IL не отрабатывает. На чужой машине предикат деградирует в статус-кво (тост придёт как раньше) — не ломается, но и не работает → **field-долг Этапа 7**.
- **`auto_lock` = `True` — осознанный дефолт** (`config.py:160` — строка устарела → актуальное в «Этап 7 / 7c»; MASTER §1 «авто-лок при отходе», тумблер в трее). ⚠️ Точность истории: поле **родилось** `True` в `a72c694` и не менялось ни разу — последовательности `true→false→true` не было; `False` у Bao — **живое dev-предпочтение** в `~/.face-unlock/config.toml`, не репо-дефолт. Риск ложных срабатываний (fullscreen-игры, idle-контенция за камеру) → **тюнинг-долг Этапа 7**.
- **`notify_service_state` / `auto_lock`** — dev-флаги регресса, в проде трогать осознанно.

**L. installer/config-дрейф → Этап 7**

- **`config.example.toml` разъехался с дефолтами кода.** Разводим два разных по тяжести случая:
  - ⚠️ **Реальный поведенческий дрейф:** `threshold = 0.45` (код: `0.32`, `config.py:45`) и `verify_required = 3` (код: `2` — `config.py:54`) — обе строки устарели → актуальное в «Этап 7 / 7c». **Свежая установка получит 0.45/3** — то есть заметно более слабый порог **и** на один обязательный матч больше (медленнее, чем задумано).
  - **Косметическая ложь (поведение НЕ меняет):** `detector_backend = "opencv"`. Кноб **инертен**: YuNet захардкожен (`detector.py:50`), а `cfg.detector_backend` читается ровно в одном месте и только чтобы напечатать (`tools/bench.py:37`). Регресса поведения тут нет — есть враньё документации.
  - **Главный дрейф — не расхождение, а умолчание:** пример объявляет 12 ключей при 48 полях `Config` → **36 ключей отсутствуют** (включая `liveness_mode`, все `adaptive_*`, все `watchdog_*`, все `pipe_*`, `auto_lock`).
- **`INSTALL.md`** устарел **в других местах**, чем пример: `:128` `threshold 0.45` и `:131` `persistent_camera false` (код: `True`, `config.py:52` — строка устарела → актуальное в «Этап 7 / 7c») — при этом `:127` `detector_backend yunet` и `:133` `verify_required 2` **верны**. Вывод: **ни один из двух доков нельзя считать эталоном**; эталон — `config.py`.
- **PyInstaller-спека тянет DeepFace-наследие:** `installer/windows_face_unlock.spec` ссылается на `collect_submodules("deepface")`/`arcface_weights.h5`, а `deepface` **отсутствует** в `requirements.txt`, `requirements.lock` и venv → сборка вероятно битая (`installer/build.py:36-38` использует `check_call`, любой ненулевой выход валит сборку). ⚠️ **Уточнения:** блок бандла весов защищён `if weights_dir.exists():`, а каталога нет → это **мёртвая конфигурация**, не безусловная ошибка; и `installer/download_weights.py` **не** является точкой отказа — он на голом `urllib` и отработает, просто скачает ~130 МБ, которые никто не потребляет. Точный режим отказа не проверен: PyInstaller в venv не установлен и нигде не запинен.
- **Мёртвый чекбокс `startuptray`** в `installer/installer.iss` — единственное вхождение, потребителей нет.
- **Дрейф четырёх копий регистрации задач** (`setup.ps1`, `installer/postinstall/register_tasks.ps1`, `tools/register_tasks.ps1`, `tools/register_watchdog_task.ps1`) → консолидация, Этап 7.

**M. Статус TODO, перенесённых из Этапа 3**

Только статус — реализации в этом этапе нет.

| TODO Этапа 3 | Статус |
|-|-|
| (a) cap одиночного `open_fast` под контенцией | → **Этап 7**, объединяется с open-дедлайном (см. §K) |
| (b) тихий режим watchdog (чёрные окна `schtasks`/CIM) | → **Этап 7**, не делалось |
| (c) читаемость раздувшегося `service.py` | **долг подтверждён: 1110 строк.** ⚠️ **НЕ рефакторить перед мержем** |
| (d) repo-wide EOL-нормализация | **частично:** `.gitattributes:8-9` пинит `config.py`/`service.py` (`text eol=lf`); полная нормализация осознанно отложена → **Этап 7** |

**N. Локи целы (git-сверка `8b7c68a..HEAD`):** в диффе ветки **НЕ изменены** `verify_frame`, `_prep_cuda_dlls`, `threshold`/`0.32`, `STRONG_MARGIN`/`0.10`, `adaptive_margin`, liveness-числа (`HF_THRESH`/`EAR_THRESH`/`SCREEN_DOUBT_FRAC`), QC/adaptive/low-light/camera/watchdog-константы, дефолты `config.py`. Литерал `0.32` встречается в диффе **только внутри описательных i18n-строк** (`field.threshold.desc`), как значение — не трогался. Серверный периметр Этапа 4 и C++-логика CP не правились: блок 7 в `credential_provider/` тронул лишь две `.lib`-записи и комментарии.

\---

## Этап 7-i — жест-раунд в Credential Provider — DONE ✅

> Ветка `stage7i-gesture` — **4 рабочих коммита**, влита в `master` **--no-ff**, merge `231ca39`. Дата closeout: 2026-07-27.
> `89aaf86` — сервер: дискриминатор `needs-gesture` + команда `unlock_gesture`.
> `8cbbef2` — парсер CP: `UnlockReply` + 12 кейсов (базовые 16 побайтово те же).
> `cdbc4d9` — async CP (вариант А): воркер, `CredentialsChanged`, selected-гейт публикации.
> `98ba9f6` — живая калибровка: `LEFT_IS_NEGATIVE_YAW` `True`→`False`.

**Итог этапа: `paranoid` стал боевым daily-driver'ом.** До окна пассивный verify в паранойе всегда возвращал `NEEDS_GESTURE`, тот схлопывался в `match:false`, и вход лицом в этом режиме был **структурно недостижим** — ветка выдачи учёток была мёртвым кодом. Теперь локскрин просит жест, ждёт и верифицирует.

**A. Дизайн: два round-trip'а, оба за SYSTEM-гейтом**

- **Фаза 1 (`unlock`)** — при вердикте `NEEDS_GESTURE` вместо неотличимого `{"reason":"no-match"}` отдаётся `{"reason":"needs-gesture","gesture","prompt","token","ttl_s","distance","real"}`. Имя вердикта берётся из уже существующего `detail["verdict"]`, поэтому форма `VerifyOutcome`, ответ `verify` и записи аудита не менялись. `.get()` намеренный: `detail` без вердикта (напр. пропуск по аренде камеры пишет `SKIPPED`) уходит на **старый** путь `no-match` — fail-closed.
- **Токен** — один слот на сервер (сервер строго последовательный, две попытки в полёте невозможны), `secrets.token_hex(16)`, изымается **до** раунда: провалившийся раунд нельзя переиграть тем же токеном. Неверный токен **не** гасит живой слот — иначе мусорный запрос отменял бы легитимный жест. `GESTURE_TOKEN_TTL_S = 15.0` — **протокольная** константа (сколько у локскрина есть на «прочитать подсказку и вернуться»), а **не** liveness-порог; проверяется только в момент запроса, поэтому начавшийся вовремя раунд ею не обрезается.
- **Фаза 2 (`unlock_gesture`)** — порядок гейтов **клон** `unlock`'а и на тех же функциях: SYSTEM → lockout → токен → камера. Сам периметр Этапа 4 не тронут (см. §J): `_build_pipe_sa`, `FIRST_PIPE_INSTANCE` и существующий гейт `unlock` в диффе отсутствуют.
- **`_release_credentials()`** — единственная точка выдачи учётных данных; оба грант-пути идут через неё, так что вопрос «откуда может уйти пароль» имеет ровно один ответ.

**B. Привязка жеста к личности**

Первая версия считала совпадения и жест **раздельно** — и это дыра: фото зачисленного лица даёт match-кадры, живой злоумышленник рядом делает движение, раунд пройден. Шов закрыт тем, что при `identity=True` кадр с `is_match=False` **вообще не доходит до `ch.feed`** — для жест-таска чужих кадров не существует. Дедлайны идут по wall-часам, поэтому не-match кадры съедают бюджет: это и есть привязка. Планка успеха — существующий `cfg.verify_required`, **новых порогов ноль**. Команда `challenge` осталась нетронутой: `identity=False` кормит таск без фильтра и отвечает байт-в-байт прежним набором ключей.

**C. async CP — вариант А**

Синхронный `GetSerialization` для жеста непригоден: раунд ждёт человека. Клик стартует воркер и сразу отдаёт `CPGSR_NO_CREDENTIAL_NOT_FINISHED`; успех сохраняется и объявляется через `CredentialsChanged`, ре-энумерация приходит с `pbAutoLogonWithDefault=TRUE`, повторный `GetSerialization` пакует из сохранённого **на потоке LogonUI**.

- `ProviderEvents` живёт в `shared_ptr` у провайдера **и** у кредла: LogonUI делает `AddRef` в `GetCredentialAt`, поэтому кредл может пережить провайдер и обратный указатель повис бы. После `Clear()` notify — просто no-op. Каждый `Release` и каждый вызов наружу (`CredentialsChanged`, `SetFieldString`) делается **вне** лока, по `AddRef`-нутому указателю: чужой код никогда не исполняется под нашим мьютексом.
- **Публикация под selected-гейтом:** сохранить учётки и получить право их объявить — **одно решение в одной критической секции**, а `SetDeselected` поднимает `m_selected=false`/`m_abort=true` под тем же мьютексом. Окна, в котором результат приземляется после ухода пользователя, не существует; брошенный результат затирается, а не остаётся в памяти.
- **`SetDeselected` без join.** Join там морозил бы локскрин до таймаута фазы 2 на рядовом сценарии «попросили моргнуть — передумал, дайте PIN». **`detach` запрещён в принципе:** живой отцепленный поток в момент выгрузки DLL из LogonUI = AV. Join остаётся только там, где блокировка легитимна — `UnAdvise` и деструктор, оба сначала снимают sink, поэтому после их возврата вызовов в LogonUI быть не может.
- `kGestureTimeoutMs = 15000` — **отдельной** константой; `kUnlockTimeoutMs = 12000` не тронут: фаза 2 ждёт человека, фаза 1 — нет, и ужатие одного не должно ужимать другое.
- `SecureZeroMemory` на консьюме, на всех брошенных/провальных путях и в деструкторе. Консьюм чистит и сохранённую копию: отказ LSA (протухший пароль) запускает новый скан, а не повтор отвергнутого пароля.

**D. Санкционированные решения архитектора (поимённо)**

- **`NEEDS_GESTURE` — не страйк; страйк переехал на исход раунда.** Числа `5`/`300` целы, `lockout.py` не тронут — сдвинут только момент записи. Обоснование страйк-фри пассива: до `NEEDS_GESTURE` доходит **только совпавшее лицо**; чужое ловит страйк на `no-match`, а фото зачисленного платит страйком за провал фазы 2. Иначе в паранойе, где эскалирует каждый распознанный кадр, бюджет из 5 попыток сгорал бы на самих вопросах.
- **Не-`ok` от движка внутри раунда → `gesture-failed` СО страйком**, наружу причина не различается; `camera-busy` остаётся собой и lockout-нейтрален, настоящая причина — в лог и аудит.
- **`camera-busy` в фазе 2 жжёт токен.** Осознанно: одноразовость держится тупой и непробиваемой, а занятая камера почти наверняка занята и для повторной фазы 1 (аренда энролла 300 с >> TTL 15 с), так что сохранённый токен всё равно ничего не спас бы.
- **`record(True)` до `load_password`** — симметрия с `unlock`, где порядок такой же; поэтому «жест прошёл, блоба нет» даёт `no-credentials` с уже сброшенным счётчиком в обеих командах одинаково.
- **`prompt` локализует сервис.** Локальный резолвер поверх `TRANSLATIONS`/`DEFAULT_LANG` в `service.py` с той же цепочкой фолбэка, что у `t()`, но с чтением `cfg.language` **на каждый запрос** — сервис не зовёт `set_language` (это делает трей, другой процесс), а `reload_config` может сменить язык на лету. Ключи `gesture.prompt.*` добавлены только в `_EN` и `_RU`; тексты самого CP остаются **EN**.
- **Балунный текст отказа заменён текстом в поле STATUS.** Раньше провал возвращал `S_FALSE` + `ppwszOptionalStatusText`; теперь провал происходит в воркере, а этот параметр отдаётся только из возврата `GetSerialization`. Строка удалена, а не оставлена мёртвой.

**E. ⭐ Ревью-гейт — продолжение §F Этапа 6, и снова класс, а не разовое**

Всё пойманное ниже — проверкой чтением/прогоном **ДО** показа диффа:

| Что | Чем поймано |
|-|-|
| `secrets.compare_digest` роняет хендлер `TypeError`'ом на не-ASCII `str` — а токен приходит **с провода** | проверка эмпирикой: `comparing strings with non-ASCII characters is not supported` → `isascii()`-гард + 5 враждебных форм токена в селфтест |
| Инъекция токена в **собственный** JSON запроса фазы 2 (кавычка/бэкслеш строят подсунутый документ) | `IsHexToken` до всякого пайпа |
| `Release` чужого sink из-под лока в `ProviderEvents::Set` — финальный `Release` исполняет чужой деструктор под нашим мьютексом | сверка со тремя остальными местами, где паттерн уже был верный |
| Вечный `m_scanning`: снятие join'а с `SetDeselected` убрало единственное место сброса → все клики после первого скана глотались бы как «уже идёт» | разбор следствий правки, а не её текста |
| Гонка «флаг до join'а предшественника»: уходящий воркер гасит `m_scanning` последним действием и затёр бы флаг нового → второй воркер | то же |

⭐ **ТАВТОЛОГИЧНЫЙ ТЕСТ НАПРАВЛЕНИЯ — главная находка окна.** `tools/liveness_selftest.py` выводил ожидаемые углы **ИЗ ПРОВЕРЯЕМОЙ КОНСТАНТЫ**:

```python
left_yaw  = NEUTRAL[POSE_YAW] + (-1 if LEFT_IS_NEGATIVE_YAW else 1) * (YAW_DELTA + 10)
```

Из-за этого кейс `TURN_LEFT fed a RIGHT turn -> FAILED (direction enforced)` — единственная проверка направления во всём репо — был **зелёным при любом знаке**. Инверсия turn-жестов дожила до боевого локскрина **не из-за отсутствия тестов, а из-за теста, который не мог упасть**. Класс дефекта, а не случай: **ожидания в тестах пишутся литералами физики и никогда не выводятся из проверяемого значения.** Оба селфтеста переписаны на литеральные знаки с комментарием «не выводить заново».

**F. Живая калибровка `LEFT_IS_NEGATIVE_YAW` `True`→`False`**

**Единственное санкционированное касание замороженной поверхности за окно** (`liveness.py:48`; остальной файл байт-в-байт). Живой факт: prompt «Поверни голову вправо», физический поворот вправо → `gesture-failed` при **`identity_frames=149`** — лицо держалось перед камерой весь раунд, распознавание было ни при чём, у цели позы был неверный знак; зеркальный поворот проходил. Конвенция позы InsightFace на этом железе даёт **положительное** отклонение yaw, когда пользователь поворачивает к **своему** левому. Проверено диагонально реальным `_PoseTask` по обоим turn-видам в обе стороны: правильное направление проходит, зеркальное — нет. Строки i18n не трогались; `prompt`/`kind`/аудит теперь означают физику пользователя.

**G. Итоги живых прогонов**

- **Сборка, на которой шла приёмка:** `build-cp\Release\FaceCredentialProvider.dll`, пересобрана Bao **2026-07-27 11:13:58** из кода `cdbc4d9`. Все локскрин-прогоны ниже шли именно на ней. Yaw-фикс `98ba9f6` — Python-only (`liveness.py` + два селфтеста), пересборки DLL не требовал, поэтому боевая DLL после него не менялась: прогоны «до» и «после» флипа отличаются только рестартом сервиса.
- **`paranoid` с реального локскрина — все 4 вида жеста** (аудит 15:19), включая честный `gesture-failed` (жест не тот, лицо держалось 149 кадров) и успешный вход следом. После флипа знака — **100%** с направлениями «по пользователю».
- **Латентность фазы 1:** warm **203–250 мс**, один холодный **1516 мс**. Раунды жеста — **1–5 с** при клиентском бюджете 15 с.
- **Токен не утекает:** не встречается ни в логах, ни в `audit.jsonl` — проверено grep'ом по живым файлам и отдельным тестом на `repr` аудит-записи.
- **`fast`-регрессия с локскрина:** автовход отработал, `CredentialsChanged` сработал живьём, деградация (второй клик) не понадобилась.
- **Фолбэк цел:** kill сервиса → вход PIN'ом с локскрина работает. `unregister`/`register` однократно — плитка исчезла и вернулась; после перерегистрации вход PIN'ом проверен отдельно.

**H. Честные остатки → ревизия на Этапе 8**

- **(а) `fast`-эскалация (`margin < 0.10`) с локскрина вживую НЕ прогнана.** Причина не в коде: галерея Bao слишком широка, `margin` держится **0.12–0.15** даже в наушниках, и порог сомнения просто не достигается. Механика делит путь с `paranoid` (доказан боем), сам триггер покрыт матрицей `liveness_verdict_selftest`. **Рецепт форса на 2 минуты:** временно `threshold = 0.26` в живом конфиге → `reload_config` → Win+L → после прогона вернуть `0.32`.
- **(б) Вотчдог-блип `ping failed 1/3`** во время ~11-секундного раунда — ожидаем и безобиден: сервер строго последовательный, пинг просто стоит в очереди. Стыкуется с ping-блипами в дельтах §2 Этапа 7.
- **(в) `cfg.blink_timeout_s` — мёртвая ручка:** `LivenessChallenge` создаётся без `timeout_s`, поэтому blink-задача берёт модульный `BLINK_TIMEOUT_S`, а конфиг входит только в wall-cap. На дефолтах оба `4.0`, наблюдаемой разницы нет. Заморожено, не тронуто.
- **(г) Сервер строго последователен на время раунда:** presence и watchdog ждут в очереди до 11 с. Вживую проблем не дало.

**I. Боевой конфиг Bao на закрытии:** `paranoid`, `threshold 0.32`, `ru`, `auto_lock=false` (dev-преференс, как и было).

**J. Локи целы (git-сверка `master..HEAD`):** в диффе ветки **НЕ изменены** `verify_frame`, `_prep_cuda_dlls`, `threshold`/`0.32`, `STRONG_MARGIN`/`0.10`, `adaptive_margin`, `HF_THRESH`/`EAR_THRESH`/`SCREEN_DOUBT_FRAC`, `YAW_DELTA`/`PITCH_DOWN_DELTA`/`GESTURE_BASELINE_FRAMES`/`BLINK_TIMEOUT_S`/`GESTURE_TIMEOUT_S`, QC/adaptive/low-light/camera/watchdog-константы, `kUnlockTimeoutMs`/`12000`. `_build_pipe_sa` и `FIRST_PIPE_INSTANCE` в дифф не попали ни одной строкой. `config.py`, `recognizer.py`, `lockout.py`, `credentials.py`, `helpers.*`, `dll.cpp`, `guid.h`, `ClassFactory.cpp`, `.def`, `CMakeLists.txt` — **вне диффа целиком**, поэтому GUID и регистрация прежние и пере-`regsvr32` не требовался. Единственное изменение в `liveness.py` — санкционированный флип §F.

---

## Этап 7 — упаковка и надёжность (в работе)

> Ветка `stage7-packaging`. Секция заполняется по мере закрытия блоков;
> полный свод — на closeout этапа.

**Эксперимент 7b-4 — клин Frame Server: НЕ воспроизведён (2026-07-28)**

Гипотеза, доставшаяся из 7a: жёсткий kill процесса с живой persistent-капчей
провоцирует клин Windows Camera Frame Server (сигнатура 2026-07-27 — LED мёртв,
`open` виснет, пайп рвётся `109`, лечится только ребутом).

- **Прогон 1 (до фикса, 19:41)** — воспроизведено ровно условие гипотезы: hard
  kill живых процессов с persistent-капчей. **Клин не наступил**, LED
  восстановился, `verify` здоров.
- **Прогон 2 (после фикса, 20:06)** — graceful-цепочка отработала: сервис вышел
  сам за **1.3 с**, kill достался только presence/watchdog (камеры не держат),
  watchdog-пауза записана (`deliberate pause is active -> not restarting`).

**Вердикт:** одиночные чистые прогоны гипотезу не опровергают — инцидент 27.07
мог быть гонкой, не воспроизводимой по требованию. Но практическая сторона
закрыта иначе: `fd84f88` ставит graceful pipe-shutdown перед hard kill в обеих
мутирующих ветках `register_tasks.ps1`, поэтому штатные `Register`/`Restart`
живую капчу больше не убивают. Прежний запрет на `clean_restart.ps1` снят.
Подробности и лестница лечения при рецидиве — `KNOWN_ISSUES.md` §2.

---

### 7c — поведение (код сдан)

> Код сдан тремя коммитами, все на `origin/stage7-packaging`. **Блок НЕ закрыт:**
> живые смоуки впереди, их доказательства лягут отдельным closeout-коммитом.

| хеш | что |
|---|---|
| `9449479` | тихий watchdog: `CREATE_NO_WINDOW` в **оба** спавна (`_run` / `_run_ps_int`), плюс наблюдаемость — ненулевой `returncode` логируется с label и 200-символьным хвостом stderr (раньше результат выбрасывался молча); два докстринг-райдера (критерий матчинга живёт в `register_tasks.ps1`, `setup.ps1` ничего не запускает сам) |
| `27fbede` | однотредовый overlapped `ping`: воркер-тред и `join` убраны, утёкшие daemon-треды исчезли классом; классы провала `busy` / `no-pipe` / `reply-timeout` / `bad-reply` / `error:<code>`; connect-retry **в пределах того же бюджета** (идиома `monitor.py` / `pipe_client.py`); `_load_config` — `Config.load()` с громким варнингом и фолбэком на дефолты, то есть вотчдог **ВПЕРВЫЕ** читает живой конфиг; read-once, без reload |
| `4fd37ff` | **оба** класса дефекта камеры (`zero-frames` / `black-burst`) → `camera-error` в presence-пробах через единый предикат `_burst_defect` (он же единственная точка чтения `camera_black_luma`); `presence_fullscreen_strikes` дефолт `10`, сигнал `SHQueryUserNotificationState` для `{QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE}`, fail-open; C-гейт `session_locked` в начале `_tick`; `config.example.toml` — 49-й ключ |

**A. Два факта рекона, изменившие скоуп блока.** Оба опровергли пункты исходного ТЗ, а не уточнили их:

- **(а) Вотчдог не читал конфиг вовсе.** `main()` строил `Config()`, а не `Config.load()`, поэтому все `watchdog_*` всегда были встроенными дефолтами независимо от `~/.face-unlock/config.toml`. Пункт ТЗ «тюнить `watchdog_ping_timeout_s` живым конфигом» был **физически недоступен**. Лечится в `27fbede` — но именно как новая возможность, а не как настройка.
- **(б) Блип — не таймаут, а мгновенный отказ коннекта.** Сервер строго последовательный и держит ОДИН инстанс пайпа (`service.py::_serve_one` пересоздаёт его на каждое соединение), поэтому занятый сервер отвечает клиенту `ERROR_PIPE_BUSY` немедленно, а в щели между `CloseHandle` и следующим `CreateNamedPipe` — `ERROR_FILE_NOT_FOUND`. Бюджет 2.0 с **не тратился**, и «поднять таймаут» не вылечило бы ничего. Настоящий рычаг — retry коннекта внутри бюджета плюс различимая причина в логе.

**B. Инвариант прописью: `busy` НЕ означает «жив».** Заклинивший-но-живой сервер (экспонат A §2 `stage-7-TZ`: инстанс навечно занят одним `verify`) держит свой единственный инстанс занятым, поэтому `ping` исчерпывает бюджет и возвращает `(False, "busy")` → `fails` растёт → на 3/3 срабатывает рестарт. Ветки, в которой `busy` даёт `True`, в коде не существует; сценарий закреплён селфтестом `tools/watchdog_ping_selftest.py`.

**C. Принятые остатки (осознанно, не долги-заглушки).**

- **Редкий блип `1/3` (`busy`) во время ~11-секундного жест-раунда** — теперь с честным `reason` в логе. Безвреден: до 2/3 не доходит, потому что раунд короче двух интервалов опроса.
- **Счётчик `fails` не затухает по времени.** Принято: любой успешный пинг сбрасывает счётчик, поэтому три провала **без единого успеха между ними** — это уже не блип, а нездоровье. Плюс цена ошибки упала: с `fd84f88` рестарт graceful-first, живую капчу он не убивает.
- **`StreamHandler` под `pythonw`** (`sys.stderr is None`, каждая запись уходит в `Handler.handleError` вхолостую). Наблюдённый факт, форма общая — та же в `service.py::_setup_logging`. Здесь не чинилось: к «чёрным окнам» отношения не имеет → кандидат 7d/7f.
- **`TF_NUM_INTRAOP_THREADS` / `TF_NUM_INTEROP_THREADS`** в `face_service/__main__.py` — по-прежнему открыт, 7d/7f.

**D. A3 — переквалификация §K и закрытие field-долга записью, без кода.** Формулировка §K («ветка `Winlogon` у medium-IL не отрабатывает», «на чужой машине не работает») **переквалифицирована**: ветка исправна и достижима там, где `ACCESS_DENIED` не стреляет первым. На medium-IL первой стреляет именно ветка access-denied — и предикат при этом **работает**, а не деградирует. Молчаливая деградация в `False` остаётся только у веток 2/3 (`remote_session.py`: любая другая win32-ошибка / не-win32 исключение) и у сборки без pywin32 — все три консервативны, `False` = статус-кво прежнего поведения. Полевая проверка на новой машине — штатным зондом `tools/session_lock_probe.py`. У предиката теперь **два** потребителя: гейт `notify_lockout` и C-гейт `_tick` (`4fd37ff`). Кода долг не потребовал.

**E. i18n-политика (принята).** Полные локали — `en` и `ru`, держатся key-for-key равными (равенство пинится селфтестом). Остальные десять — best-effort на цепочке фолбэка `t()`: язык → английский → сырой ключ. Новые поля конфига получают ключи **только** `en` + `ru`. Вариант «добить» (~1160 машинных строк) **отклонён** по цене/качеству: непроверенный машинный перевод формулировок про безопасность хуже честного английского фолбэка. Политика записана комментарием в шапке `face_service/i18n.py` (правка comment-only, AST до/после идентичен).

**F. Устаревшие адреса строк в исторических разделах Этапа 6.** Текст тех разделов не переписывается — он датирован Этапом 6; у каждой устаревшей ссылки стоит только пометка «строка устарела → актуальное в «Этап 7 / 7c»». Поиском найдено **шесть** таких мест, **все шесть помечены**. Актуальные адреса на HEAD `4fd37ff` (`config.py` сдвигался в 7a-C, снявшем три поля, и в 7c-3, добавившем одно):

| поле | где | в тексте | актуальный |
|---|---|---|---|
| `threshold` | §L | `config.py:45` | **`config.py:43`** |
| `verify_required` | §L (та же строка) | `config.py:54` | **`config.py:51`** |
| `persistent_camera` | §L | `config.py:52` | **`config.py:49`** |
| `persistent_camera` | Этап 6 §E | `config.py:52` | **`config.py:49`** |
| `auto_lock` | §K | `config.py:160` | **`config.py:186`** |
| `camera_open_timeout_s` | §K | `config.py:122` | **`config.py:126`** |
| `pipe_unlock_require_system` | Этап 6 §D | `config.py:150` | **`config.py:176`** |

Семь ссылок на шести строках: `threshold` и `verify_required` живут в одной. Прочие адреса в тех же разделах (`service.py:*`, `camera_open.py:*`, `detector.py:50`, `monitor.py:180`) в этом заходе НЕ пересчитывались — они не входили в санкцию 7c-4 и остаются датированными Этапом 6.

**G. 7c-5 — диагностируемость presence-пробы (`a0874ce`, log-only).** Проба получала полный возврат `verify_frame` `(is_match, distance, is_real)` и выбрасывала две трети: в `service.log` лежал только `request cmd=presence` **без исхода**, поэтому ложный `absent` был структурно неразбираем. Теперь кортеж накапливается по каждому проанализированному кадру и на пути `absent` пишется `log.info` с классификацией, выведенной из самого кортежа (`recognizer.py:369-383`): `no-face` (`d≥1.0`, `real=F`), `anti-screen` (`d<1.0`, `real=F`), `above-threshold` (`real=T`), `engine-error` (`analyze_frame` бросил). На пути `present` — та же форма в `log.debug`, чтобы здоровая машина не писала строку каждые 60 с. Detection-проба получила симметричную пару без дистанций (YuNet отвечает только да/нет). **Control flow, числа, конфиг и `verify_frame` не тронуты**: единственные новые ветвления выбирают уровень лога, оба возвращают то же значение. Форматтеры вынесены на уровень модуля намеренно — `tools/presence_guards_selftest.py` исполняет реальное тело пробы на stub-объекте, и методы заставили бы тест их зеркалировать (первая версия именно так и упала).

**H. Инцидент 19:13–19:24 — ложные absent-локи при сидящем пользователе (ОТКРЫТ).** Машина уходила в лок по presence-страйкам, пока пользователь сидел перед камерой и распознавался: в `19:24:09` `verify` дал `matches=5/2`, `best=0.165`, `sceneL=126.9` (все пять кадров — матч, дистанция вдвое лучше порога), а через **секунду**, в `19:24:10`, проба вернула `absent`, счётчик добрал двойку и сработал `LockWorkStation`. Ручные `presence` в том же окне (`19:23:56`) давали `present:true` ×3. `auto_lock` возвращён в `false` (подтверждено grep'ом по живому конфигу) — дальнейших локов быть не должно, но **причина не установлена**. Рекон вскрыл асимметрию проб: presence снимает 2 сброса + до **3** рабочих кадров (оба числа — хардкод-литералы, с `verify` ничего не разделяют), требует **ОДИН** матч по тому же порогу `0.32` с early-exit, а анти-экран гасит матч **жёстко** на уровне кадра, тогда как у `verify` тот же сигнал — лишь один голос многокадрового вердикта. `real` в решении монитора не участвует вовсе. Три равновероятные гипотезы (`no-face` / `anti-screen` / `stale-frames`) различаются одной строкой нового лога 7c-5. Полный разбор фактов, гипотез и плана — `stage-7-TZ.md` §2; дизайн фикса (7c-6) **не начинается до улик репро**.

**I. 7c-6 — трёхуровневый вердикт пробы (`7e4c88b` / `7136427` / `21a3f7a` / `4a26da7`).** Кадр классифицируется как `strong` / `weak` / `suspect` / `none` / `error`, бёрст сворачивается в `present` / `uncertain` / `absent`. Ни одно число распознавания не двинуто: порог читается из конфига, `presence_soft_margin=0.05` — **только** для пробы, `verify_frame` не тронут. `uncertain` сам по себе не лочит; серия `presence_uncertain_streak=3` конвертится в страйк, а `absent` подтверждается ре-пробой через `presence_confirm_delay_s=2.0`. **Живая приёмка 2026-08-04:** uncertain-строки в лок НЕ конвертились (15:48, 15:56, 15:57, 16:20); пойман Д4-ретракт (15:51 `absent`→`present`, страйк не потрачен); честный уход по-прежнему даёт лок за 65–124 с. Оба механизма инцидента 01.08 закрыты своими ветками: анти-экран на близкой дистанции → `suspect`→`uncertain`; хвост рабочей позы за порогом → `weak`→`present`.

**J. 7c-7 — гейт залоченной сессии (`86efc46` + `cbeed5b`).**

⚠️ **КОРРЕКЦИЯ СОБСТВЕННОЙ ЗАПИСИ 7c-4 (внесена 2026-08-04).** Пункт **D** выше и строка `:313` утверждают, что зонд «на реальном локскрине подтвердил обе ветки предиката» `session_locked`. **Это опровергнуто данными.** `session_lock_probe.log` за 2026-07-20 содержит 610 сэмплов с шагом 2 с: **609** — `session_locked=False desktop='Default'`, и **один** `True` с `desktop='?(error)'`, зажатый `False` с обеих сторон в двухсекундном окне, то есть транзиентный блип, а не лок. Сэмплов `desktop='Winlogon'` в файле **нет ни одного**. Исходные формулировки оставлены как есть и помечены здесь: аудит-запись, принятая без улик, — сама по себе дефект, и стирать её значит стирать урок.

Практическое следствие было в проде: гейт стоял первым оператором `_tick` начиная с `4fd37ff`, но предикат под ним возвращал `False` на локскрине, поэтому пробы шли **внутри залоченной сессии**, а заработанный там страйк переживал разлок — `presence.log` 01.08: лок 17:32:47 → проба 17:33:47 в локе → повторный лок 17:34:52, примерно через 40 с после входа лицом. Та же форма видна 2026-07-18, за две недели до появления гейта, — значит это не регресс 7c-6, гейт здесь просто никогда не срабатывал.

Детектор заменён на `WTSQuerySessionInformation(WTSSessionInfoEx)` → `WTSINFOEX_LEVEL1.SessionFlags`: сессию спрашивают о ней самой, а не выводят ответ из хэндла десктопа. **Живая проба 2026-08-04 15:36–15:37** (`wts_verify.log`): `False`→`True`→`False` точно по рёбрам лока, нулевой дребезг. Независимое подтверждение пришло с неожиданной стороны: `lockout_notify_selftest` упал на непатченном детекторе — это возможно **только** если WTS вернул реальный ответ, потому что при `None` код ушёл бы в фолбэк на пропатченный стаб и тест бы прошёл. Ребро `locked`→`unlocked` даёт полный сброс счётчиков — ремень к пряжке гейта, покрывающий и ручной Win+L. Тем же коммитом `cbeed5b` на авторитетный детектор переведён гейт тоста `notify_lockout`: он существовал ровно затем, чтобы балун не сгорал на локскрине, и старый предикат его обессмысливал.

**K. 7c-8 — input-fusion (`003a162`).** Камера перестала быть единственным свидетелем. Порядок гейтов строго `session` → `input` → `camera`; живой ввод свежее `presence_input_idle_s=45.0` закрывает тик как `present` **без единого `pipe_call`**. Ввод может закончить тик только присутствием, никогда отсутствием, а провал или безмнение зонда уходят в камеру, а не утверждают присутствие. Арифметика `GetTickCount` — беззнаковая 32-битная: и `tick`, и `dwTime` заворачиваются каждые ~49.7 суток, а обычное вычитание после заворота уходит в большой минус и читалось бы как «ввод только что» ещё 49.7 суток, молча отключая блокировку; маскирование чинит заодно и знаковое чтение ctypes. 18 пинов, включая обе стороны границы `2**32`. **Живая приёмка 2026-08-04 16:48–17:09:** 20 минут `src=input` без единого камерного голоса, корректная передача слова камере на 45 с простоя, уход = лок за 162 с при расчётном окне 105–170 с. **Осознанное решение:** гейт стоит до status-полла, поэтому при активном вводе тосты ждут 45 с простоя — задержка, не потеря; `poke_events()` покрывает Save/Resume. Попутно заменена мёртвая `_get_idle_seconds`: в ней был ровно этот wrap-баг и возврат `0.0` («ввод только что») на провале, то есть fail-open в сторону «никогда не лочить».

**L. Канон счёта живых процессов (исправлен 2026-08-04).** Считать фильтром по `CommandLine -match "face_service|presence_monitor|tools\.watchdog"` среди `python*`, и **через ~10 с после рестарта**: `Start-ScheduledTask` асинхронный, немедленный счёт даёт недобор. Старый канон фильтровал по пути репозитория и ловил только шимы: воркеры под `Python312` наследуют командную строку без пути venv. Старая формула дала два ложных срабатывания за 04.08.

**M. Аномалия дат в логах (разведка 7c-7 / RE).** Часы машины прыгали в течение сессии — примерно на сутки вперёд между блоками работы (похоже на ресинк после гибернации), плюс расхождение TZ: git писал `-0400`, Git-Bash рапортует `VST`. Отсюда строки с телеметрией 7c-6 под датой 31.07 при коде от 01.08. **Порядок строк в логах достоверен, абсолютные даты между рестартами — нет.** Проверка часо-зависимых таймеров: TTL жест-токена **монотонный** (`service.py:907` ставит `time.monotonic() + GESTURE_TOKEN_TTL_S`, проверка `:921`) — прыжок часов ему безразличен; аренда камеры энроллом — **wall-clock** (`service.py:1165` `time.time() + max(5.0, seconds)`, проверка `:434`), то есть скачок часов вперёд обрывает аренду досрочно, назад — продлевает. Записано кандидатом в `KNOWN_ISSUES`, оценка в 7d; сейчас не чинилось.

**N. Разовый срыв graceful shutdown 16:15 (2026-08-04).** Пайп-клиент не уложился в таймаут, цепочка ушла в hard kill живого владельца persistent-капчи — ровно условие гипотезы `KNOWN_ISSUES §2`. Камера пережила, сигнатуры №2 нет. Ещё один негативный прогон гипотезы; запись §2 не закрывается.

**O. Переопределение критерия S3.** Исходная формулировка («сидеть в кадре и не получить лок») была нереалистична для RGB-камеры без ИК: рабочая поза, взгляд в телефон и наклон вниз штатно выводят лицо из кадра. Критерий переопределён как **«живые механизмы работы не конвертятся в лок»** — именно он и проверялся живьём 04.08 по 7c-6 и 7c-8.

**P. Процесс.** Верификация 7c-3 (`py_compile` + `config_example_selftest` + `presence_guards_selftest`) выполнена **рантайм-исполнителем (Bao)** по санкции архитектора: классификатор прав на PowerShell был недоступен, обход другим инструментом запрещён стоячим правилом, серия из трёх попыток с паузами исчерпана. Критерии зелёные (49 == 49, `FAILURES: 0`), коммит сделан по подтверждению.

---

### 7d — ядро упаковки (закрыт 2026-08-05)

> Серия `0ce12ae..ec08984` — 13 коммитов (12 кода + один фикс по живому прогону),
> все на `origin/stage7-packaging`. `Config` = **53 поля**, ни одного нового ключа.
> Селфтесты: 22 из 22 зелёные (20 прежних + `config_validation_selftest` +
> `packaging_selftest`).

| хеш | фаза | что |
|---|---|---|
| `0ce12ae` | A | валидация на пути загрузки + выживание битого конфига |
| `e1b88e7` | B | одна ротируемая настройка логов, `StreamHandler` только при живом `stderr` |
| `41a0a00` | C | аренда камеры на монотонных часах (`KNOWN_ISSUES §3`) |
| `e0c924a` | C | удаление вестигиальных `TF_NUM_*` |
| `d91b7d9` | D | матчер вотчдога следует раскладке |
| `ae251bd` | E | роутер флагов фрозен-трея + GUI-диалог пароля |
| `05cb428` | F | `buffalo_l` едет с инсталлятором, честный отказ без него |
| `9de81e7` | G | апдейтер: свой форк, обязательная сумма, apply только под frozen |
| `5948f78` | H | третья frozen-точка входа + флип `InstalledExe` |
| `65e99ab` | I | двухфазный uninstaller |
| `2a19dd8` | J | подпись CP-DLL |
| `9c0123b` | K | фикс спеки после первой реальной сборки |
| `ec08984` | F1 | `-ErrorAction` не делает чтение реестра безопасным под StrictMode |

**Q. Д1 и Д2 противоречили друг другу; развязано флагом, а не компромиссом.** Д1
требовала «битый конфиг → полные дефолты», Д2 — «reload отвечает `invalid-config`
и держит старый конфиг». В лоб несовместимо: после Д1 `Config.load()` вернул бы
дефолты, `validate()` на них прошла бы, и **reload молча подменил бы живой конфиг
дефолтами** — ровно та тихая подмена, против которой Д2 и написана. Введён
`Config.load(strict=False)` (`config.py:225`): холодный старт деградирует громко,
`_reload_config` зовёт `strict=True` (`service.py:1115`) и получает `ValueError`.
Обе Д-точки выполнены буквально, ни одна не ослаблена. Проверка симметрии — 18
кейсов `tools/config_validation_selftest.py`, каждый в подпроцессе с
`FACE_UNLOCK_HOME` во временном каталоге; ребёнок отказывается работать, если
`APP_DIR` не равен выданному пути.

**R. Валидация переехала на путь загрузки — и это НЕ бесплатно.** `validate()` не
чистая: при `pipe_hardened_sd=True` она импортирует pywin32 и резолвит SID
текущего юзера (`config.py:411`). Раньше эту цену платили три места (reload, GUI
Save, смена языка), теперь — каждый из 20 `Config.load()`. Принято сознательно:
альтернатива (вынести SID-проверку из `validate()`) меняла бы её контракт и для
reload-пути тоже. Кандидат на кэш SID вынесен в 7f. **Следствие для
эксплуатации:** конфиг, не прошедший новую валидацию, уводит сервис на дефолты
молча для пользователя и громко для лога — см. пункт W про якорь грепа.

**S. `validate()` добрала 11 полей, ни одна существующая граница не сдвинута.**
`distance_metric` (енум единственного реализованного значения — кнобка объявлена с
Этапа 0 и не читается ничем, `config.py:42`), `camera_index`,
`camera_warmup_frames`, `verify_frames`, `verify_required` + кросс-проверка
`verify_required <= verify_frames`, и real-bool для `persistent_camera`,
`warmup_on_start`, `challenge_on_doubt`, `audit_log`, `anti_screen`,
`adaptive_gallery`. Последний проверяется **вне** ветки `if self.adaptive_gallery:`
намеренно: truthy не-bool раньше сам решал эту ветку и не проверялся никогда.
`verify_required > verify_frames` — не настройка, а разлочка, которая не может
случиться (`service.py:625` крутит `range(verify_frames)`, сравнивает с
`verify_required` на `:662`).

**T. Дефект `StreamHandler` оказался не тем, чем его записали в 7c.** Парковка
7c гласила «каждая запись уходит в `Handler.handleError` вхолостую». Точнее:
`StreamHandler` привязывает `sys.stderr` **на конструировании**
(`logging/__init__.py:1133`), под pythonw он становится `None`, `emit` ловит
`AttributeError` и зовёт `handleError`, а тот сам загорожен
`if raiseExceptions and sys.stderr:` (`:1078`). То есть исключение **не всплывает
и ничего не печатается**, а ни одна строка лога не теряется — `FileHandler` стоит
первым в списке и отрабатывает до падения. Цена — холостой `format()` плюс
конструирование исключения на каждую запись в четырёх долгоживущих процессах.
Починено условным хендлером в `face_service/logging_setup.py`; заодно все четыре
файла получили ротацию 5 MiB × 2, которой не было ни у одного (у `audit.jsonl`
она есть с Этапа 2). `tools/bench.py` не тронут осознанно: dev-only, настраивает
логи на уровне импорта модуля, и это единственная точка входа, у которой консоль
есть всегда.

**U. Дефект watchdog-Installed — не тот, что предполагался, и порядок фикса
обратный порядку слов в ТЗ.** Живость приходит из пайп-пинга
(`tools/watchdog.py:345`), который режимо-независим и в Installed **корректен**.
Ломалось всё, что ниже вердикта: kill сигналил ноль процессов, death-wait затем
писал `all killed processes confirmed gone` — **ложное подтверждение** ровно той
гарантии, ради которой шаг существует, — а посмертный счёт выживших был
структурно приколочен к нулю, поэтому `restart_outcome` возвращал
`unrecoverable` независимо от реальности. Инертный рестарт плюс гарантированно
неверный диагноз, цикл 120 с. Флип `InstalledExe` поэтому сделан **последним**
кодовым коммитом (`5948f78`), после mode-aware матчера (`d91b7d9`): флип первым
превратил бы честное «супервизора нет» в нечестное «супервизор есть и не
супервизирует». Попутно найден близнец того же класса вне скоупа:
`tools/clean_restart.ps1` звал регистратор без `-Mode`, то есть на установленной
машине «чистый рестарт» тихо вырождался в «запусти таски заново».

**V. Первая реальная сборка прошла с `exit 0` и была нерабочей.** Коммит
`d43786f` предсказывал, что первая сборка потребует правки, и ожидал её в
CUDA-дереве. Правка потребовалась в графе импортов:
`face_service/__main__.py` берёт код через `from .service import main` —
**относительный** импорт, а PyInstaller анализирует точку входа как top-level
скрипт без пакетного контекста, поэтому modulegraph такой импорт не резолвит и
**молча выбрасывает**: ни строки в `warn-*.txt`, exit 0. В бандле лежали
`config`, `i18n`, `detector`, `logging_setup` (приходят через явные
hiddenimports) и **не лежали** `service`, `recognizer`, `camera`, `camera_open`,
`liveness`, `lockout`, `audit`, `adaptive` — половина приложения, ради запуска
которой точка входа существует. Найдено разбором `PYZ-00.toc`; смоук не
потребовался и был бы запрещён правилами блока. `presence_monitor` спасся
случайно: та же нерезолвимая строка, но `monitor` затащил `enroll_gui`, который
**есть** в hiddenimports. Обе точки названы явно в `9c0123b`. Итог сборки:
`dist\WindowsFaceUnlock` — 2066 файлов, 3.39 GiB, три exe по ~29.9 МБ; 2.4 GiB из
них `_internal\nvidia`, 325 MiB — распакованный `buffalo_l` ровно там, где его
ищет `recognizer.model_root()`.

**W. Поправка якоря грепа для доков.** Живой L1 искал в `service.log` по
`failed validation|unknown key`. Отрендеренные строки всех пяти путей
`Config.load` показали, что этот якорь ловит **2 из 5**: строку валидации и
warning про неизвестные ключи. Три ERROR-пути он пропускает — битый TOML,
не-UTF-8 и IO-ошибку, то есть ровно те случаи, когда сервис уехал на дефолты. У
всех четырёх ERROR общий суффикс `-- falling back to built-in defaults`.
Канонический якорь: `'falling back to built-in defaults|unknown key'`.

**X. F1: `-ErrorAction` не делает чтение реестра безопасным (`ec08984`).** Первый
живой `-DryRun` `uninstall.ps1` умер на секции «Installed layout» с
`PropertyNotFoundStrict`, инвентарь оборван, шесть секций не напечатаны, побочек
нет. Идиома
`(Get-ItemProperty -Path $k -Name $n -ErrorAction SilentlyContinue).$n` выглядит
защищённой и не является ею: `-ErrorAction` гасит **ошибку**, но при отсутствующем
ключе cmdlet возвращает `$null`, а `$null.Anything` под
`Set-StrictMode -Version Latest` **терминирующая**. То есть на любой машине, где
продукт никогда не ставился, чтение убивало скрипт. AST-свип всех четырёх `.ps1`
дал **три** сайта класса: живой (`uninstall.ps1`, `InstallLocation`) и два
латентных — `(default)` у `InprocServer32` в том же файле (выстрелил бы на
полу-зарегистрированном CLSID, ровно на состоянии, которое аниснталлер и чистит)
и HKLM-фолбэк в `register_tasks.ps1`, добавленный фазой E, байт-в-байт то же
выражение, ждавшее `-Mode Installed`. Введён `Get-RegValueOrNull` (щупает и
объект, и свойство; проверен на трёх состояниях: ключа нет / значения нет / оба
есть). Сверх бага: каждая секция идёт через `Invoke-Section`, поэтому падение
стоит одной секции и печатается как `UNKNOWN`, а `$fuFound` пред-заполнен для
всех ключей — иначе верификация фазы B споткнулась бы о дыру. AST-инвариант
пере-доказан: выход фазы A на `363`, баннер фазы B на `369`, мутирующие вызовы на
`401` и `413`, выше баннера — ноль. Свип заодно **снял** три ложных подозрения:
`(Get-Date).AddYears(3)` (`Get-Date` не может вернуть `$null`), `foreach` по
`$null` (в PowerShell ноль итераций, не ошибка) и `$fuFound[...]` (индексация, а
не доступ к свойству).

**Y. Живая приёмка 7d (Bao, 2026-08-05) — L1–L6.**

| # | что проверялось | результат |
|---|---|---|
| L1 | ротация логов + тихий рестарт | `clean_restart` graceful **1.7 с**, 6 процессов, ротация сработала живьём: `service.log.1` = 11.1 MB, свежий `service.log` = 4.7 KB; чёрных окон нет |
| L2 | валидация конфига на живом сервисе | неизвестный ключ → WARNING **с именем** (`zzz_bogus_key`, 15:10:26); битый TOML → reload вернул `ok:false` `"invalid-config"`, **сервис жив на старом конфиге**; восстановление чистое |
| L3 | монотонная аренда камеры | четыре ре-арма, каденс **45–47 с** (15:11:05→15:13:23), закрытие `camera lease cleared (103.8s still remained)` → `KNOWN_ISSUES §3` закрыт живьём |
| L4 | первый живой `register.ps1 -Action register -DryRun` | чистый префлайт: Release-DLL FOUND, буквальная команда `regsvr32`, оба ключа PRESENT, **ноль изменений** |
| L5 | `sign_cp.ps1 -SelfSigned` | DLL подписана, `CN=Windows Face Unlock (development)`, thumbprint `13F9BB…649C`; статус `UnknownError` **без** `-TrustLocally` — ожидаемо, в сторы ничего не ставилось; после подписи вход лицом с локскрина **×2** под `paranoid`, регресса нет |
| L6 | `uninstall.ps1 -DryRun` после F1 | инвентарь **до конца**: 3 таски Running, оба куста CP, Installed layout absent без краша, `.venv` 3.42 GiB, `build-cp` с CLSID-предупреждением, данные 66 файлов / 15.8 MiB и **4 поимённые сироты** (`credentials.bin.bak`, `credentials.bin.pre-stage4`, `embeddings.npz.bak`, `wts_verify.log`), `.insightface` 600.7 MiB SHARED, `%TEMP%`-хвост absent; ноль изменений |

L5 попутно даёт улику к рамке `credential_provider/SIGNING.md`: вход лицом
работает при подписи, которой система **не доверяет** (`UnknownError`, сертификат
в сторы не клался). Это прямое подтверждение тезиса, что подпись не гейтит
загрузку Credential Provider.

**Z. Сюрприз конфига: `auto_lock = true` пережил смоуки 7c.** Обнаружен по эху
`reload` в L2: в живом `config.toml` стоял `auto_lock = true` — остаток смоуков
S3/S4 от 04.08, который не был возвращён в исходное тем же заходом. Флип на
`false` выполнен py-однострочником + рестарт; валидация нового файла молчит (греп
по якорю из **W** даёт только историческую строку L2). **Процессный урок, а не
дефект кода:** флаг, поднятый ради смоука, обязан опускаться **в том же заходе**,
которым поднят, иначе он живёт до следующей случайной находки — здесь это были
почти сутки. Ни один селфтест такого поймать не может: значение легальное,
валидацию проходит, и отличается от намерения только тем, что намерение нигде не
записано.

**AA. Что из скоупа 7d НЕ доделано и перенесено.** Апдейт-механизм жив только в
Installed-раскладке — dev по конструкции notify-only (`updater.py`: apply-путь
под `sys.frozen`, иначе ссылка на релизы), потому что запуск инсталлятора на
чекауте поставил бы вторую копию и перенацелил таски на неё. `setup-exe` не
собран: ISCC на машине отсутствует (проверены три стандартных пути, `PATH` и
`INNO_SETUP_ISCC`) → в 7f.

**Гипотезы, не факты.** (1) Фрозен-раскладка даёт `sys.stderr is None` — вывод по
флагам спеки `console=False, windowed=True`, рантайм бандла не наблюдался.
(2) Installed-путь рестарта вотчдога не исполнялся ни разу: exe собран, матчер
режимо-осведомлён, но живого прогона на установленной машине не было — в
`tasks.psd1` это записано прямо в комментарии. (3) `absl-py` и `astunparse` в
`requirements.lock` — предположительно осиротевшие транзитивные пины TF-эпохи;
граф зависимостей не резолвился.

\---

### 7e — same-user disclosure и SYSTEM-custody: РЕШЕНИЕ (2026-08-06)

> Скоуп: **7e-R1** (рекон по коду), **7e-1** (гигиена секретов в деинсталляторе),
> **7e-2** (храповик posture на `reload_config`), **7e-3** (эта запись + MASTER-TZ §6/§7).
> Итог: same-user disclosure — **ПРИНЯТО**; TPM-seal — **ПРИНЯТО-НЕ-ОТЛОЖЕНО**. Обе позиции
> закрыты здесь, а не переданы на ревизию Этапа 8 очередным «design-item».

**A. Таксономия: четыре пути, две разные природы.** Рекон 7e-R1 разложил общую формулировку
«same-user disclosure» на конкретные пути по коду. Делить их надо не по тяжести, а по природе:
два следуют из принципала, под которым живёт сервис, и не убираются ничем кроме смены этого
принципала; два были просто грязью и убираются как грязь.

| # | Путь на этом коде | Природа |
|---|-------------------|---------|
| 1 | Прямое чтение `credentials.bin` + `CryptUnprotectData` с энтропией из `pipe_entropy.bin`; оба файла читаемы своим юзером по построению (`credentials.py:58`, `:128`) | **ФУНДАМЕНТАЛЬНЫЙ** |
| 2 | Осиротевшие v1-копии блоба, расшифровываются **печатной** константой `ENTROPY = b"face-unlock:v1"` (`credentials.py:33`) — per-install энтропия им не нужна вовсе | **ГИГИЕНА** |
| 3 | Флип posture-ключа в `config.toml` (пишется юзером) + негейченный `reload_config` → `unlock` как SELF мимо SYSTEM-гейта (`service.py:1301`, `:1387`) | **ГИГИЕНА** |
| 4 | Чтение памяти сервиса: тот же юзер, Medium IL, `RunLevel Limited` (`register_tasks.ps1:254-255`) → `OpenProcess(PROCESS_VM_READ)` по plaintext в момент выдачи | **ФУНДАМЕНТАЛЬНЫЙ** |

⚠️ **Пути 2 и 3 в прошлых аудитах не были выписаны явно** — ни в threat-model Этапа 4
(«прочитал `credentials.bin` + вызвал `CryptUnprotectData`» — это только путь 1), ни на
closeout'е Этапа 5. Оба найдены реконом 7e-R1. Путь 2 при этом уже был **виден живьём**:
приёмка 7d L6 напечатала `credentials.bin.bak` и `credentials.bin.pre-stage4` среди «4
поимённых сирот» — но как безымянный мусор, а не как расшифровываемый креденшл. Строка отчёта
была, прочтения не было.

**B. Почему «частичного custody» не бывает: граница принципал-based и потому бинарна.**
Ключ либо у SYSTEM — и тогда юзер его не достаёт, — либо доступен процессу юзера, и тогда
достаёт. Промежуточной величины нет. Конкретно на этом коде: сервис крутится под limited-user
(`register_tasks.ps1:254-255`), значит он обязан САМ расшифровать блоб, значит энтропия обязана
быть user-читаемой, значит юзер читает её тем же вызовом. Любая схема, оставляющая расшифровку
в user-процессе, воспроизводит путь 1 целиком, как её ни усложняй. «Решение на 60 %» здесь —
не компромисс, который поленились взять, а несуществующая величина.

**C. Принятие путей 1 и 4 — явное, с обоснованием.**

- **Потолок проекта ниже Hello ПО ДИЗАЙНУ** (MASTER §1, «честный потолок»): convenience-grade+
  на RGB без ИК и анклава. Несменяемый пол — PIN/пароль, который остаётся живым фолбэком всегда.
  Защита не падает ниже пароля; она и не поднимается выше него.
- **Local admin уже вне модели** (threat-model Этапа 4, «bypass-by-design»). Атакующий, который
  УЖЕ исполняет код под этой учёткой в этой сессии, получает от блоба маргинальный довесок к
  тому, чем и так владеет: живой сессией, файлами, токеном.
- **Цена непропорциональна и порождает новый класс отказов там, где отказ дороже всего.**
  SYSTEM-custody — это отдельный привилегированный компонент на пути логона. Его отказ = отказ
  входа, против MASTER §1 («всё обратимо, вход по PIN/паролю всегда остаётся»). Мы уже дважды
  ловили ровно такой класс дефекта на границе SYSTEM/limited-user: серверный SID-резолвер
  (`bab8705`) и клиентская проверка доверия (`e527ab6`) — оба ломали ЖИВОЙ локскрин и оба были
  видны только на живом прогоне.
- **Путь 4 не закрывается в принципе на этом принципале.** Плюс: см. G(1) — зануление памяти в
  CPython не работает.

**D. 7e-1 — гигиена: деинсталлятор опознаёт осиротевшие копии секретов.**

- Было: `$fuKnownData` — список ТОЧНЫХ имён, `$fuSecret` — хардкод двух имён. Копия секрета не
  совпадала ни с одним и падала в «UNMANAGED (no writer anywhere in this repo)» рядом с
  логами. Стало: `$fuSecretStems` + `Test-FuSecretFile` (`uninstall.ps1:145`, `:164`) —
  классификация по **стему с любым суффиксом**, не по перечню имён, чтобы третий вариант
  (`.tmp`, `.old`, что угодно) не промахнулся так же, как промахнулись эти два. Стемы
  **изъяты** из `$fuKnownData`: один файл — один классификатор, дублирования нет.
- Три точки применения: SECRETS-репорт enumerates каталог и классифицирует
  (`uninstall.ps1:346`), осиротевшие копии печатаются отдельным подсписком с размерами,
  «unknown»-фильтр исключает секреты ПЕРВЫМ (`:376`).
- ⚠️ **Семантика, которую легко переоценить:** классификация — **репорт-онли**. Удаления
  по-файлам в этом скрипте нет вообще: данные сносятся каталогом целиком
  (`Remove-Trace $fuDataDir` под `-RemoveData`), поэтому копии удалялись реальным прогоном и
  ДО этой правки. Правка меняет не удаление, а **видимость**: оператор, запускающий фазу A,
  теперь видит их как креденшлы, а не как мусор. Расширять удаление на отдельные файлы вне
  `-RemoveData` осознанно НЕ стали — это сломало бы контракт «Data is KEPT by default».
- **Доказательство dry-run-безопасности стало исполняемым артефактом:**
  `tools/uninstall_dryrun_proof.ps1` парсит `uninstall.ps1` (`Parser::ParseFile`, без запуска) и
  проверяет четыре вещи: гейт `exit` внутри `if (-not $Force)` на верхнем уровне; транзитивную
  достижимость функций до гейта; мутирующие команды/методы по денай-листу; и **всякую**
  косвенность `& $x` как мутирующую по умолчанию, с одним разрешаемым паттерном (`& $Body`, где
  каждый call-site передаёт литеральный скриптблок — тот уже просканирован на месте). До 7e-1
  этот инвариант пере-доказывался руками при каждой правке (см. 7d §X); теперь он запускается.
- **Живой факт, требующий РУЧНОГО шага Bao:** в `~/.face-unlock` лежат `credentials.bin.bak` и
  `credentials.bin.pre-stage4`, по 300 B, от 2026-07-07 04:42:24. Живой v2-блоб — 303 B (ровно
  +3 байта префикса `b"v2:"`), то есть копии — **v1**. Авторитетен только `credentials.bin`;
  копии подлежат удалению вручную. Ни один файл репо их не создаёт и не создавал.

**E. 7e-2 — храповик posture: САНКЦИОНИРОВАННОЕ усиление периметра Этапа 4.**

- Периметр Этапа 4 заморожен; эта правка — усиление, санкционированное в рамках 7e, и она
  меняет **lifecycle reload** двух существующих bool-полей, не трогая ни сравнение гейта, ни
  `_build_pipe_sa`, ни замороженные числа.
- Механизм: `POSTURE_KEYS` (`service.py:136`), boot-значения фиксируются в `_posture_floor`
  в `__init__` (`:458`), `_apply_posture_ratchet` (`:1139`) вызывается из `_reload_config`
  **до** свапа `self.cfg` (`:1185`). True→False отвергается по-ключевно с
  `log.warning("posture downgrade refused: %s stays True")`, False→True применяется, пол
  поднимается. Остальная часть reload идёт штатно — иначе отравленный posture-ключ стал бы
  способом блокировать легитимный reload всего прочего.
- Пол — не только boot-значение, а максимум виденного: иначе ключ спускался бы в два шага
  (`False→True` reload, затем `True→False`). Селфтест это проверяет отдельным кейсом.
- **БЕЗУСЛОВЕН: конфиг-поля для отключения НЕТ** — оно жило бы в том самом файле, которому
  храповик не доверяет. Следствие: число полей `Config` не изменилось (53), так что
  `config_example_selftest` и `presence_guards_selftest` по длине не двигались.
- Аудит вызывающих (стоп-условие шага 0) прошёл чисто: `presence_monitor/gui.py:379-383` и
  `tray.py:241`/`:393` зовут `reload_config`, но `_collect` (`gui.py:357-365`) стартует с
  `Config.load()` и переписывает только поля, у которых ЕСТЬ виджет; ни одного виджета для
  posture-ключей нет (репо-греп: в `gui.py` эти имена не встречаются вообще). Селфтесты задают
  ключи через конструктор `Config(...)`, а не через reload. Легитимного пути, который штатно
  ослаблял бы ключ через reload, в коде нет.
- ⚠️ **Одна дев-оговорка, честно:** `credential_provider/tests/unlock_harness.cpp:17-18`
  документирует `pipe_unlock_require_system = FALSE` как предусловие живого харнеса. Оно
  применяется **рестартом** сервиса (boot-значение = пол храповика), и так харнес продолжает
  работать. Если же кто-то попробует добиться того же `reload_config`'ом на работающем
  хардненном сервисе — получит отказ. Это ожидаемое поведение, а не регресс; отдельный кейс
  селфтеста фиксирует, что boot-soft сервис остаётся soft.

**F. TPM-seal — ПРИНЯТО-НЕ-ОТЛОЖЕНО.** Тройное основание, каждое достаточно само по себе:
(а) обёртки NCrypt / Platform Crypto Provider в pywin32 нет — только через `ctypes`, нетривиально
(греп по репо: символов `NCrypt*` в коде **ноль**, TPM живёт исключительно в доках);
(б) без custody TPM бесполезен — тот же юзер всё равно расшифрует;
(в) **с** custody он тоже почти ничего не добавляет: SYSTEM-scope DPAPI уже machine-bound, и
единственный остаточный выигрыш TPM — стойкость к извлечению мастер-ключа SYSTEM, то есть против
SYSTEM/admin, то есть вне модели. Пункт (в) — новый; прежние формулировки («берём в Этап 5
вместе с custody») останавливались на (б) и потому оставляли TPM висеть как долг.

**G. Два полу-решения, рассмотренных и ОТВЕРГНУТЫХ (чтобы не всплывали снова).**

1. **Зануление пароля в памяти сервиса — театр.** В CPython `str` неизменяем; к моменту, когда
   код мог бы «занулить» его, интерпретатор уже сделал неотслеживаемое число копий (`json.dumps`,
   буфер `WriteFile`, аллокаторные арены), и ни одна из них не адресуема из Python. Сравнение с
   C++-стороной обманчиво: `ZeroString` в `FaceCredential.cpp` работает потому, что там `std::wstring`
   с известным буфером. Реализация на python-стороне создала бы **видимость** контрмеры против
   пути 4, не сдвинув его ни на шаг.
2. **Частичный custody без SYSTEM-компонента невозможен** — см. B. Все обсуждавшиеся варианты
   (энтропия в отдельном user-процессе; ACL на энтропию строже; расшифровка в дочернем процессе)
   упираются в одно: пока расшифровывающий принципал — тот же юзер, путь 1 воспроизводится
   целиком. Единственная вещь, которая двигала бы границу, — расшифровка ПОД SYSTEM, то есть
   полный custody, отвергнутый в C по цене.

**Прогоны 7e-2 (venv 3.12, `PYTHONUTF8=1`, camera-free, temp-home):**
`pipe_hardening_selftest` — **OK**, 27 проверок (12 из них — новый блок `[5] posture ratchet`);
`config_example_selftest` — **OK** (53 ключа == 53 поля `Config`, полей не добавлялось);
`presence_guards_selftest` — **OK**, 0 провалов. Регресс: `gesture_round_selftest` — **OK**,
`config_validation_selftest` — **OK**. `uninstall.ps1` живьём **не запускался** (стоячая
санкция); AST-прувер выполнен и держится: гейт на строке 414, мутирующих/неразрешённых
косвенных вызовов выше — ноль.

**H. Addendum 7e-4 — биометрия: СТОП сработал, и нашёл больше, чем искали.**

Задача ставилась узко: добавить `embeddings.npz*` отдельной категорией по образцу 7e-1. В
задание было вписано стоп-условие — «если найдётся сырой биометрический материал сверх
`embeddings.npz*`, СТОП, не расширяй классификатор сам». **Оно сработало.** Инвентаризация
продакшн-кода дала три вещи, которых в постановке не было:

| Что | Кто пишет | file:line | На диске (2026-08-06) |
|-----|-----------|-----------|------------------------|
| `adaptive.npz` — **второй файл шаблона** | `AdaptiveStore.save` → `np.savez(embeddings=…)` | `adaptive.py:150-158`, путь `config.py:25` | отсутствует (кольцо пусто), но лежал в `$fuKnownData` как рядовой файл |
| `enroll\enroll_*.jpg` — **сырые полные кадры**, не кропы | **шиппед-визард**, `cv2.imwrite` | `enroll_gui.py:698-700`; дублируется в dev-CLI `enroll.py:35-36` | 30 файлов, ~2.1 MB |
| `enroll\_qc_crops\*_crop.png` — **выровненные кропы лица** | dev-зонд под `--save-crops` | `enroll_qc_probe.py:99-102`, каталог `:66`, дефолт `--dir` = `ENROLL_DIR` `:39` | 15 файлов, ~305 KB |

**Главный дефект оказался не в классификаторе, а в ОБХОДЕ.** Все три скана инвентаря —
секреты, счётчик энролла, unknown-фильтр — шли `Get-ChildItem -File` **без** `-Recurse`. Значит
подкаталог был невидим не одному списку, а **всем трём одновременно**: 15 изображений лица не
попадали ни в «BIOMETRIC: N enrollment image(s)», ни в UNMANAGED, при том что отчёт продолжал
утверждать, что называет всё. Это ровно тот же класс дефекта, что путь 2 в разделе A — вещь
лежала на виду и была не прочитана, — но с более неприятным корнем: там подводил перечень имён,
здесь подводила форма обхода, а такой дефект не чинится добавлением ещё одного имени в список.

**Что сделано.**

- **Один классификатор на обе категории.** `Test-FuSecretFile` (7e-1) заменён на
  `Get-FuDataClass`: принимает путь ОТНОСИТЕЛЬНО каталога данных, возвращает
  `'keymaterial'` | `'biometric:template'` | `'biometric:image'` | `$null`. Параллельной
  почти-одинаковой функции не заведено — SECRETS-блок, BIOMETRIC-блок и unknown-фильтр
  деривятся из одного ответа, поэтому файл физически не может быть чувствительным для одного
  списка и невидимым для другого.
- **Два источника категории biometric.** По стему на верхнем уровне: `embeddings.npz*`,
  `adaptive.npz*` → `template`. **По расположению**, рекурсивно и на любой глубине: всё внутри
  `enroll\` → `image`. Правило по имени в принципе не поймало бы `_qc_crops`, потому что там
  имена произвольные — категорию задаёт место.
- **Порядок правил нагружен смыслом:** key material проверяется ПЕРВЫМ (копия креденшла,
  оказавшаяся внутри `enroll\`, обязана остаться креденшлом), затем расположение, затем стем.
  Оба порядко-зависимых кейса зафиксированы проверкой классификатора.
- **`adaptive.npz` изъят из `$fuKnownData`** и переехал в biometric — не задвоен. Это меняет
  только метку в инвентаре; по факту удаления для него ничего не изменилось.
- **Обход стал одним рекурсивным проходом**, из которого выведены все списки. `enroll`-счётчик
  как отдельный механизм удалён — учёт объединён, задвоения нет.
- **Читаемость отчёта — требование, а не побочный эффект.** Шаблоны печатаются поимённо с
  размерами (осиротевшую копию надо уметь опознать, чтобы удалить); изображения — счётчиком с
  суммой, **сгруппированным по подкаталогу**, чтобы `_qc_crops\` был отдельной строкой, а не
  растворялся в счётчике энролла, как раньше. 45 путей построчно отчёт бы похоронили.
- **Арифметика печатается каждый прогон:** `accounted: N secret + N template + N image + N known
  + N unmanaged = N of N file(s)`. Каждый файл попадает ровно в одну корзину по построению, так
  что «ничего не выпало из инвентаря» становится проверяемым взглядом, а не обещанием.
  Расхождение с общим счётом каталога — само по себе находка.
- **Staleness НЕ вычисляется.** Соблазн был: таймстемпы в именах кропов (`1783419812818…`)
  относятся к более старому поколению, чем текущие кадры (`1784512…`/`1784557…`), и по ним
  напрашивалась пометка «остаток прошлого энролла». Не делаем: это эвристика по имени файла,
  которая соврёт при первой же смене формата. Задача — сделать всё **видимым**, а решение о
  том, что стало мусором, принимает человек.

**Аргумент, ради которого категория отдельная.** Пароль можно сменить — поэтому оставленная
копия блоба это плохо, но обратимо. Лицо сменить нельзя, поэтому оставленный на диске шаблон
или фотография — остаток, который не закрывается никогда. Внутри самой биометрии есть градация:
сырой кадр приватнее вектора, потому что это оригинал, а не производная. Сливать это в один
«sensitive»-бакет с секретами нельзя — более слабое утверждение («секреты убраны») подменило бы
более сильное, а MASTER §7 требует «без следов» и для второго тоже.

**Контракт цел.** По-файлового удаления не добавлено ни для секретов, ни для биометрии.
Удаление остаётся whole-dir под `-RemoveData` (`Remove-Trace $fuDataDir`); рекурсия обхода на
это не влияет никак — она изменила только то, что попадает в инвентарь. «Data is KEPT by
default» держится буквально.

**Верификация 7e-4.** `tools/uninstall_dryrun_proof.ps1` перезапущен на изменённом файле:
**PROOF HOLDS**, гейт на строке 502, `Get-FuDataClass` в reachable-set и доказана чистой,
мутирующих вызовов выше гейта — ноль, все четыре (`& $fuRegistrar`, `& $fuCpScript`, два
`Remove-Item`) ниже. Рекурсивный `Get-ChildItem` мутирующих вызовов не внёс: он read-only и в
находках не появляется. Сам классификатор проверен отдельно — извлечён из AST и исполнен как
функция (без единого top-level-стейтмента скрипта) на 13 кейсах, включая оба порядко-зависимых:
**0 провалов**. `uninstall.ps1` живьём по-прежнему не запускался.

**Живой факт:** `credentials.bin.bak` и `credentials.bin.pre-stage4` Bao удалил вручную
2026-08-06 — в каталоге их больше нет, остался только авторитетный `credentials.bin` (303 B, v2).
`embeddings.npz.bak` (31 568 B, 2026-07-07) на диске остаётся и теперь классифицируется как
`biometric:template`; ни один файл репо его не создаёт.
