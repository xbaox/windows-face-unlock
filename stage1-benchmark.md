# stage1-benchmark.md - Этап 1: движок ONNX/InsightFace (GPU)

> Форк xbaox/windows-face-unlock, ветка stage1-onnx-engine, дата 2026-07-07
> Железо: ASUS TUF FA507XI, Win11, RTX 4070, RGB-вебка (index 0), Python 3.12.10 (.venv)

## Движок
- Было (сток): DeepFace ArcFace через tf-keras, **CPU** (нативный Windows-TF GPU не видит) + MiniFASNet-liveness (torch CPU) + YuNet-детект.
- Стало: **InsightFace buffalo_l** (YuNet/SCRFD detect + w600k_r50 ArcFace embed), **onnxruntime-gpu 1.26.0 + CUDA 12.9 + cuDNN 9.24**, CUDAExecutionProvider. Liveness (MiniFASNet) пока оставлен как есть -> уходит на Этапе 2.
- det_size=640, эмбеддинг 512-D (L2-normed), cosine distance (1 - cos_sim), метрика и порог из cfg.

## Стек и ключевой фикс
- onnxruntime-gpu 1.27 требует CUDA 13 (pip-редиста нет) -> pip сам откатил на **1.26.0 + cu12**; драйвер UMD 13.3 обратно-совместим. Запинено onnxruntime-gpu==1.26.0.
- **Критично:** cuDNN 9 подгружает сублибы (cudnn_engines_tensor_ir64_9.dll и др.) по ИМЕНИ в рантайме; для venv pip-инсталла папки нет в DLL-search-path -> CUDA молча падает на CPU. Фикс: os.add_dll_directory(nvidia/*/bin) + ctypes-preload всех cuDNN DLL по полному пути ДО создания сессий. Вшито в Recognizer._prep_cuda_dlls(). ort.preload_dlls() в одиночку НЕ спасает.

## Латентность (end-to-end verify через Recognizer.verify_frame, N=10, на себе)
| | сток (CPU-TF) | Этап 1 (GPU) | выигрыш |
|---|---|---|---|
| avg verify | ~0.67 s | **0.040 s** | ~16.8x |
| min verify | 0.24 s | 0.035 s | |
| max verify | 0.81 s | 0.073 s | |

- Чистый движок (detect+align+embed, без liveness/пайпа, onnx_probe): detect 12.7ms + embed 5.1ms = **avg 18ms**.
- Первый кадр без прогрева liveness давал спайк ~6.3s (ленивая загрузка TF/MiniFASNet). Добавлен прогрев liveness в _lazy_app -> первый кадр 73ms.

## Точность (на себе, норм. свет)
- self-distance: **0.063-0.080** (avg 0.070) при пороге 0.45. Приёмка <=0.35 -> бита с запасом ~5x. Margin до порога 0.37.
- match=True 10/10, real=True 10/10.
- (Шкала InsightFace отличается от DeepFace: сток на себе давал 0.024-0.033 в своём пространстве; сравнивать модули нельзя, важен запас до порога.)

## Прогрев (старт сервиса)
- Чистый InsightFace: **1.62 s** (< 5s -> цель этапа по движку достигнута; против 17.8s стока).
- С прогревом liveness (TF-загрузка): 7.27 s. Это ВРЕМЕННЫЙ размен: холодный старт +5.6s один раз vs спайк 6.3s на первом боевом verify каждый раз. TF-liveness и этот прогрев УХОДЯТ на Этапе 2 (активный liveness). Не подгонялось.

## Приёмка Этапа 1
- [x] onnxruntime-gpu видит CUDAExecutionProvider, реально считает на GPU (не тихий CPU-фолбэк).
- [x] Recognizer переписан на InsightFace, сигнатуры enroll_from_dir/load/verify_frame не менялись; service.py не тронут.
- [x] Ре-энролл на 512-D (15/15), версионирование .npz (engine=insightface-buffalo_l, dim=512), старый DeepFace-файл отвергается внятно.
- [x] avg verify <= 0.67s (0.040s, ~16.8x); distance на себе <= 0.35 (0.070); прогрев движка < 5s (1.62s).
- [ ] Регресс (Шаг 8): фото с телефона отшивается (liveness не сломан), presence, пайп ping/status -> СЛЕДУЮЩИЙ шаг.
- [x] Локскрин не тронут; страховки на месте.

## Открытые пункты в Этап 2 / позже
- Порог: self-dist ~0.08 при 0.45 -> после снятия чужого/фото (Шаг 8) поджать к ~0.35 (безопасность). Решается на числах.
- Два детекта на кадр (DeepFace-liveness + InsightFace-embed) -> уйдёт с активным liveness на Этапе 2.
- 3 лишние модели buffalo_l (1k3d68/2d106det/genderage) грузятся и игнорятся (allowed_modules отсекает использование; файлы не удаляли).
- System Protection на C: НЕ трогать до Этапа 7.
