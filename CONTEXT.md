# HBM РЕПЕТИТОР — CONTEXT.md
# Последнее обновление: 27.02.2026

---

## ИНСТРУКЦИЯ ДЛЯ НОВОГО ЧАТА

**Не читай файлы целиком.** Вся архитектура описана ниже. Читай файлы только если нужно менять конкретный участок кода.

### Карта файлов — что где лежит

**Бэкенд (корень HBM/):**

| Файл | Что внутри | Когда читать |
|---|---|---|
| `database.py` | 20 строк. DATABASE_URL, engine, SessionLocal, get_db() | Только если меняешь подключение к БД |
| `auth.py` | ~60 строк. hash_password(), verify_password(), create_token(), decode_token(), get_current_user(), require_tutor() | Если добавляешь новые роли или меняешь авторизацию |
| `models.py` | ~120 строк. SQLAlchemy модели: User, Student, Section, Item, Attachment, Board, parent_student_link. Enum'ы: GoalType, FormatType, ItemType, TopicStatus, ControlStatus, UserRole | Если добавляешь новую таблицу или поле |
| `schemas.py` | ~170 строк. Pydantic: LoginRequest/Response, ChangePasswordRequest, UserCreate/Out, StudentCreate/Update/Out/ListItem, SectionCreate/Update/Out, ItemCreate/Update/Out, AttachmentOut, BoardOut | Если добавляешь новый эндпоинт |
| `main.py` | ~750 строк. Все эндпоинты FastAPI + WebSocket handler доски. Шаблоны (TEMPLATES dict). Board CRUD + WS. Хелперы очистки файлов. Раздача static/ (без catch-all mount!) и uploads/ | Основной файл для бэкенд-изменений |
| `init_db.py` | 30 строк. create_all() + создание аккаунта admin | Трогать редко |

**Фронтенд (static/):**

| Файл | Что внутри | Когда читать |
|---|---|---|
| `api.js` | ~60 строк. getToken(), requireAuth(), logout(), apiFetch(), apiGet/Post/Patch/Delete/Upload() | Если меняешь логику авторизации на фронте |
| `login.html` | Форма входа + форма смены пароля при первом входе. Редирект по роли: tutor→hbm_tutor, student→student_lk, parent→parent_lk | Если добавляешь новую роль |
| `hbm_tutor.html` | ЛК репетитора. Список учеников (карточки с прогрессом). Модалка добавления ученика. Модалка 👥+ приглашения. Копирование данных для входа | UI репетитора |
| `student.html` | Профиль ученика (для репетитора). Полное редактирование: roadmap, лента, конструктор, перемещение ↑↓, вложения, ИДЗ, контрольная, шаблоны. Кнопка «📐 Доска». Проверяет isTutor — скрывает кнопки для не-репетиторов | Самый большой файл. Читай только нужную функцию |
| `student_lk.html` | ЛК ученика. Read-only профиль + кнопка «📐 Доска» | Если меняешь что видит ученик |
| `parent_lk.html` | ЛК родителя. Список детей → клик → профиль. Read-only | Если меняешь что видит родитель |
| `board.html` | Электронная доска (~350 строк). Canvas + WebSocket. Инструменты: Курсор/Перо/Прямая/Ластик/Текст. Панель свойств. Real-time sync | Всё что касается доски |

### Ключевые паттерны

- **Все fetch-запросы** идут через `api.js` → `apiFetch()` добавляет `Authorization: Bearer <token>` автоматически. При 401 — редирект на login.
- **Роли проверяются дважды:** на фронте (`requireAuth(['tutor'])`) и на бэкенде (`Depends(require_tutor)` или `check_student_access()`).
- **Расчёт прогресса** дублируется на фронте (в каждом HTML) — функции `calcBlockScore(sec)` и `calcOverall(secs)`. Формула: среднее из (ДЗ avg, ИДЗ/5, контрольная). Скидка = floor(progress% × 0.3).
- **student.html** используется И репетитором И учеником/родителем (переход с hbm_tutor.html). Флаг `isTutor = getRole() === 'tutor'` управляет видимостью кнопок редактирования.
- **Статика раздаётся** через отдельные роуты `@app.get("/{page}.html")` — НЕ через `app.mount("/", StaticFiles(...))`, т.к. catch-all mount блокирует WebSocket.
- **Доска** использует WebSocket. Undo удаляет последний объект **этого пользователя** (по `user_id`). Объекты хранятся в `boards.strokes` как JSON.

### Как добавлять новое

**Новый эндпоинт:** schemas.py (схема) → main.py (роут) → фронт (вызов через apiGet/apiPost)
**Новая таблица:** models.py (модель) → init_db.py (она подхватится автоматически через create_all) → python init_db.py
**Новая страница:** создать HTML в static/, добавить `<script src="api.js">`, вызвать `requireAuth()` в начале
**Новое поле у ученика:** models.py (Column) → schemas.py (поле в Create/Update/Out) → main.py (если нужна логика) → фронт (отображение)

---

## О ПРОЕКТЕ
Платформа управления учениками для репетитора по математике из Великого Новгорода.
Цель: масштабировать доход с 70к до 200к/месяц без увеличения часов.

---

## ТЕКУЩАЯ ВЕРСИЯ: v2.0

### Стек
- **Бэкенд:** FastAPI + SQLAlchemy + PostgreSQL 16 (localhost:5432, БД `hbm`)
- **Авторизация:** bcrypt (хэши паролей) + JWT (PyJWT)
- **WebSocket:** FastAPI WebSocket + uvicorn[standard]
- **Фронтенд:** Vanilla JS, CSS-переменные, тёмно-зелёная тема
- **Запуск:** `uvicorn main:app --reload --port 8000` из папки HBM

### Структура файлов
```
C:\Users\Lenovo LEGION\Downloads\HBM\
├── database.py
├── models.py
├── schemas.py
├── auth.py
├── main.py
├── init_db.py
├── requirements.txt
├── .gitignore
├── README.md
├── CONTEXT.md
├── uploads/
├── static/
│   ├── login.html
│   ├── api.js
│   ├── hbm_tutor.html
│   ├── student.html
│   ├── student_lk.html
│   ├── parent_lk.html
│   └── board.html
```

### Таблицы БД
1. **users** — id, login, password_hash, role (tutor/parent/student), name, must_change_password, student_id (FK), created_at
2. **students** — id, name, grade, goal, base_rate, format, created_at
3. **sections** — id, student_id (FK), title, position, is_open, idz_enabled, control_enabled, idz, control
4. **items** — id, section_id (FK), type (topic/hw/note), position, name, status, total, done, closed, date, closed_date, note, text
5. **attachments** — id, item_id (FK), name, mime, size, file_path
6. **parent_student_link** — parent_id (FK users), student_id (FK students) — many-to-many
7. **boards** — id, student_id (FK, unique), strokes (TEXT/JSON), created_at, updated_at

### API эндпоинты
**Auth:** POST /api/auth/login, POST /api/auth/change-password, GET /api/auth/me
**Users (репетитор):** GET /api/users, POST /api/users, DELETE /api/users/{id}
**Students:** GET /api/students, GET /api/students/{id}, POST /api/students, PATCH /api/students/{id}, DELETE /api/students/{id}
**Sections:** POST /api/students/{id}/sections, PATCH /api/sections/{id}, DELETE /api/sections/{id}
**Items:** POST /api/sections/{id}/items, PATCH /api/items/{id}, DELETE /api/items/{id}, POST /api/sections/{id}/items/reorder
**Attachments:** POST /api/items/{id}/attachments, DELETE /api/attachments/{id}
**Templates:** POST /api/students/{id}/apply-template/{oge|ege|olymp}
**Board:** GET /api/boards/{student_id}, WS /ws/board/{student_id}

### Электронная доска
- Real-time Canvas + WebSocket
- Инструменты: Курсор (Esc), Перо (P), Прямая (L), Ластик (E), Текст (T)
- Ctrl+Z — undo **только своих** объектов (по user_id)
- Панель свойств: цвет, толщина, пунктир, B/I/U, редактирование текста
- Прямые: перетаскивание концов, привязка к 45° (Shift)
- RGB color picker
- Тёмная/светлая тема
- Скролл: тяг в режиме Курсор / Alt+Click / средняя кнопка / колёсико (зум)

### Пакеты Python
fastapi, uvicorn[standard], sqlalchemy, psycopg2-binary, pyjwt, bcrypt, python-multipart

### Подключение к БД
```
postgresql://postgres:ПАРОЛЬ@127.0.0.1:5432/hbm
```
(127.0.0.1, не localhost — обход проблемы с IPv6)

---

## БЭКЛОГ

### Ближайшее
- [ ] История занятий (таблица sessions)
- [ ] Привязка родителя к нескольким детям через UI (пока только через Swagger)
- [ ] Дополнительные геометрические примитивы на доске (окружности, стрелки)

### Среднесрочное
- [ ] Хостинг (вынос из localhost)
- [ ] Смена JWT-секрета на переменную окружения
- [ ] S3 для файлов вместо локальной папки uploads/
- [ ] Уведомления (о новых ДЗ, оценках)
- [ ] Alembic для миграций БД

### Долгосрочное
- [ ] Расписание занятий / календарь
- [ ] Онлайн-оплата
- [ ] Аналитика и отчёты
- [ ] Мобильное приложение
