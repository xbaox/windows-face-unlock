# audit-notes.md — Этап 0, аудит стока `caochitam/windows-face-unlock`

> Форк: `xbaox/windows-face-unlock` · ветка `stage0-audit` · дата: 2026-07-07
> Слабые места стока + карта «куда бьём» по этапам. Прикладывать к окнам следующих этапов.

---

## ⚠️ СКВОЗНОЕ НАПОМИНАНИЕ (тащить через все этапы)
**Защита системы (System Protection / точка восстановления) на диске C: включена намеренно как страховка от лок-аута.**
**Отключить её можно ТОЛЬКО на Этапе 7 — и только после того, как вход лицом с локскрина собран, работает и протестирован.**
До Этапа 7 не выключать. Финальное окно (Этап 7) обязано напомнить Bao это сделать явным пунктом приёмки.

---

## 1. Канал (named pipe) — КРИТИЧНО → Этап 4

Файл: `face_service/service.py`

| # | Что | Где | Риск | Куда бьём |
|---|-----|-----|------|-----------|
| C1 | **NULL DACL** пайпа: `sd.SetSecurityDescriptorDacl(1, None, 0)` | `service.py:57–64` (`_build_sa_everyone`), применяется `:296` | К пайпу коннектится ЛЮБОЙ локальный процесс под твоей учёткой | Этап 4: DACL = SELF (текущий юзер) + SYSTEM (LogonUI). Больше никого. |
| C2 | **Нет анти-сквоттинга**: `PIPE_UNLIMITED_INSTANCES` | `service.py:301` | Вредонос поднимает свой пайп с тем же именем → перехват коннектов CP / фишинг | Этап 4: `FIRST_PIPE_INSTANCE` + проверка, что сервер — наш (мьютекс уже есть, но это не защита канала). |
| C3 | **Пароль отдаётся в открытом виде** на `unlock` при матче | `service.py:280–291` (`_handle`, ветка `unlock`) | В связке с C1: чужой процесс шлёт `unlock`, ждёт, пока ты перед камерой, забирает пароль Windows | Этап 4: nonce от CP → сервис подписывает ответ ключом, недоступным чужим; DACL (C1) отсекает чужих клиентов. |
| C4 | **Нет nonce / подписи ответа** вообще | весь `_handle` | Реплей: записал ответ сервиса — воспроизвёл | Этап 4: challenge-response, nonce одноразовый, подпись. |
| C5 | Команды `shutdown`, `pause_camera`, `resume_camera`, `reload_config` без авторизации | `service.py:240–260` | Любой процесс глушит сервис / уводит камеру → DoS входа лицом | Этап 4: те же DACL + подпись закрывают; опц. — allowlist команд для не-SYSTEM клиента. |

Команды сервиса (для справки, `_handle`): `ping`, `status`, `reload_config`, `shutdown`, `pause_camera`, `resume_camera`, `build_enrollment`, `verify`, `presence`, `unlock`.

**Есть и хорошее:** single-instance mutex `Local\FaceUnlockService` в `serve_forever` — два сервиса не конкурируют за пайп (но это про стабильность, не про безопасность).

---

## 2. Хранение пароля → Этап 4 (опц. TPM)

Файл: `face_service/credentials.py`

- Схема: DPAPI **user-scope** (`win32crypt.CryptProtectData`), блоб `{u,p,d}` в `%USERPROFILE%\.face-unlock\creds.dat` (см. `CREDS_PATH`).
- **Дыра:** `ENTROPY = b"face-unlock:v1"` — захардкожен и лежит публично в опенсорсе. Дополнительная энтропия должна быть секретом; здесь она известна всем → барьер против таргетированного вредоноса **под твоей же учёткой** ≈ 0.
- Против других юзеров машины и оффлайн-атаки DPAPI user-scope держит нормально (то, что и заявлено в докстринге файла — честно).
- Куда бьём: Этап 4 (advanced) — TPM-sealing DPAPI-блоба; как минимум — вынести энтропию в машинно-уникальный секрет, а не хардкод.

---

## 3. Движок распознавания → Этап 1

Файлы: `face_service/recognizer.py`, `face_service/detector.py`, `requirements.txt`, `config.example.toml`

- Сток на **DeepFace**:
  - эмбеддинг — `DeepFace.represent(model_name="ArcFace")` → тянет **`tf-keras` / TensorFlow** (тяжёлый импорт).
  - живость — `DeepFace.extract_faces(anti_spoofing=True)` → **MiniFASNet через `torch`/`torchvision`**.
- Детектор по умолчанию — `detector_backend = "opencv"` (Хаар-каскады: медленно + неточно на углах/свете).
- **Двойной прогон детектора на каждый кадр:** в `verify_frame` сначала `extract_faces` (детект + анти-спуф), потом `represent` (снова детект + выравнивание + эмбеддинг). Детектор гоняется дважды по одному кадру.
- Метрика: cosine distance, `threshold = 0.45` (ниже = строже).
- Куда бьём (Этап 1): полный ONNX/InsightFace `buffalo_l` — **YuNet detect + ArcFace embed** на onnxruntime-GPU (RTX 4070); один детект на кадр; эмбеддинг только по выровненному кропу; прогрев моделей; early-exit на первом уверенном матче; graceful CPU-фолбэк. Числа бенчмарка стока (Шаг 9) — база для сравнения.

Зависимости стока (`requirements.txt`): `deepface`, `opencv-python`, `numpy`, `tf-keras`, `pywin32`, `psutil`, `torch`, `torchvision`, `pystray`, `Pillow`, `tomli/tomli-w`. На Этапе 1 связка `deepface + tf-keras + torch` уходит в пользу `onnxruntime-gpu + insightface`.

---

## 4. Живость (liveness) → Этап 2

- Сейчас: только **пассивный** MiniFASNet (`is_real`). Плоское фото держит средне, видео-реплей с телефона и статичный экран — слабо.
- Куда бьём (Этап 2): активный челлендж — blink-детект (EAR по лэндмаркам) + рандомный микро-жест как эскалация; анти-экран детектор (муар/блики/рамка); режимы **Быстрый** (челлендж при сомнении, дефолт) / **Параноик** (челлендж всегда).

---

## 5. Rate-limit / lockout / аудит → Этап 2

- В стоке **нет** ни rate-limit, ни lockout после N провалов лицом, ни аудит-лога попыток (есть только обычный `logging` в `%USERPROFILE%\.face-unlock\...log`).
- Куда бьём (Этап 2): лимит попыток + временный lockout лица → только PIN; аудит-лог каждой попытки (успех/провал, скор, вердикт liveness, время).

---

## 6. GUID → Шаг 4 (сейчас, Этап 0)

Захардкожен `{F8A0B4D9-3C7F-4B0A-9E21-8C1B1E2B7C10}`. Места:

| Файл | Строка | Форма | Менять? |
|------|--------|-------|---------|
| `credential_provider/guid.h` | 4 (коммент), 6–7 (`DEFINE_GUID`) | байтовая | **да** |
| `credential_provider/dll.cpp` | 42 | строковая `{...}` (self-register в реестр) | **да** |
| `credential_provider/dll.cpp` | 60 | строковая `{...}` (unregister) | **да** |
| `credential_provider/FaceCredential.cpp` | 141 | макрос `CLSID_FaceCredentialProvider` | нет (тянет из guid.h) |
| `credential_provider/dll.cpp` | 23 | макрос | нет |

Реестр пишется самим DLL (`regsvr32` → `DllRegisterServer` в `dll.cpp`), отдельного захардкоженного реестрового пути в `register.ps1` нет — источник строкового GUID один: `dll.cpp:42,60`. Байтовая (`guid.h`) и строковая (`dll.cpp`) формы **обязаны совпадать**.

> Правка чисто текстовая. Компиляция C++ — только Этап 5. На Этапе 0 просто вписываем новый GUID корректно в оба формата, чтобы на Этапе 5 собралось.

---

## 7. Конфиг — knobs (`config.example.toml`)

| Knob | Дефолт | Смысл |
|------|--------|-------|
| `model_name` | `ArcFace` | DeepFace-модель (уйдёт в ONNX на Этапе 1) |
| `detector_backend` | `opencv` | детектор (→ YuNet) |
| `distance_metric` | `cosine` | метрика |
| `threshold` | `0.45` | cosine distance, ниже = строже |
| `anti_spoofing` | `true` | MiniFASNet |
| `camera_index` | `0` | индекс вебки |
| `verify_frames` | `5` | кадров на verify |
| `verify_required` | `3` | матчей из verify_frames для успеха |
| `presence_interval_s` | `60` | период проверки присутствия |
| `presence_absent_strikes` | `2` | «страйков» отсутствия до авто-лока |
| `presence_mode` | `recognition` | `recognition` (должен совпасть) / `detection` (любое лицо) |
| `warmup_on_start` | `true` | прогрев моделей при старте |

---

## 8. Карта «куда бьём» (сводно)

- **Шаг 4 (сейчас):** свой GUID → `guid.h` + `dll.cpp:42,60`.
- **Этап 1:** движок ONNX/InsightFace (YuNet+ArcFace, GPU), один детект, early-exit, CPU-фолбэк. База — числа бенчмарка стока (Шаг 9).
- **Этап 2:** активный liveness (blink+жест), анти-экран, rate-limit+lockout, аудит-лог, режимы Быстрый/Параноик.
- **Этап 3:** надёжность (мульти-условный энролл, адаптивная галерея, низкий свет, занятая камера, watchdog).
- **Этап 4:** харднинг канала — DACL SELF+SYSTEM (C1), FIRST_PIPE_INSTANCE (C2), nonce+подпись (C3,C4), закрыть DoS-команды (C5); опц. TPM-seal пароля (§2).
- **Этап 5:** Credential Provider (C++, VS2022+CMake), свой GUID, защищённый протокол, таймаут, фолбэк, RDP-off, регистрация. ⚠️ ЛОКСКРИН.
- **Этап 6:** UX (визард PySide6, трей, RU/EN).
- **Этап 7:** упаковка/подпись/uninstaller/чеклист + **напомнить Bao выключить System Protection**.
