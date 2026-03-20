# HBM Репетитор

Платформа управления учениками для репетитора по математике.
Прогресс-трекинг, домашние задания, расписание, мессенджер, тарификация со скидками, электронная доска с real-time совместным рисованием и системой координат.

---

## Содержание

- [Быстрый старт](#быстрый-старт)
- [Архитектура](#архитектура)
- [Структура файлов](#структура-файлов)
- [База данных](#база-данных)
- [API](#api)
- [Авторизация и роли](#авторизация-и-роли)
- [Фронтенд — страницы](#фронтенд--страницы)
- [Расписание](#расписание)
- [Мессенджер](#мессенджер)
- [Курсы и программа](#курсы-и-программа)
- [Электронная доска](#электронная-доска)
- [Бизнес-логика](#бизнес-логика)
- [Развёртывание](#развёртывание)

---

## Быстрый старт

### Требования

- Python 3.10+
- PostgreSQL 16+
- Node.js не требуется (фронтенд без сборки)

### Установка

```bash
# 1. Клонировать репозиторий
git clone https://github.com/beereolf-started/hbm-tutor.git
cd hbm-tutor

# 2. Установить зависимости
pip install -r requirements.txt

# 3. Создать базу данных PostgreSQL
psql -U postgres -c "CREATE DATABASE hbm;"

# 4. Создать таблицы + аккаунт репетитора
python init_db.py

# 5. Запустить сервер
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### Первый вход

Открыть `http://127.0.0.1:8000` (используйте `127.0.0.1`, не `localhost`).

| Поле   | Значение   |
|--------|------------|
| Логин  | `admin`    |
| Пароль | `admin123` |

При первом входе система потребует сменить пароль.

---

## Архитектура

```
┌─────────────────┐     HTTP/WS      ┌─────────────────┐     SQL     ┌──────────────┐
│   Браузер        │ ◄──────────────► │  FastAPI         │ ◄─────────► │  PostgreSQL   │
│   (Vanilla JS)   │                  │  + WebSocket     │             │  (hbm)        │
└─────────────────┘                   └─────────────────┘             └──────────────┘
```

| Слой        | Технология                                       |
|-------------|--------------------------------------------------|
| Бэкенд      | FastAPI + SQLAlchemy ORM                         |
| СУБД        | PostgreSQL 16                                    |
| Авторизация | bcrypt (пароли) + JWT/PyJWT (72ч, HS256)        |
| WebSocket   | FastAPI WebSocket + uvicorn[standard]            |
| Фронтенд    | Vanilla JS, CSS-переменные, без фреймворков      |

**Ключевые принципы:**
- Нулевые зависимости на фронте — ни React, ни Vue, чистый JS
- Один HTML файл = одна страница
- WebSocket для real-time доски
- JWT в `localStorage` (`token`, `role`, `name`)
- Роли проверяются дважды — фронт (UX) + бэкенд (безопасность)

---

## Структура файлов

```
HBM/
├── database.py          # Подключение к PostgreSQL
├── auth.py              # bcrypt + JWT + get_current_user + is_tr()
├── models.py            # SQLAlchemy модели
├── schemas.py           # Pydantic-схемы (v2)
├── main.py              # FastAPI, все эндпоинты, WebSocket
├── init_db.py           # Создание таблиц + аккаунт admin
├── requirements.txt
├── uploads/             # Загруженные файлы (создаётся автоматически)
└── static/
    ├── api.js            # Общий модуль: token, fetch-хелперы, logout
    ├── login.html        # Вход + смена пароля
    ├── hbm_tutor.html    # ЛК репетитора (owner/tutor)
    ├── student.html      # Профиль ученика — редактирование (репетитор)
    ├── student_lk.html   # ЛК ученика — просмотр, расписание, курсы
    ├── parent_lk.html    # ЛК родителя
    ├── profile.html      # Личный профиль пользователя
    ├── board.html        # Электронная доска (Canvas + WebSocket)
    ├── courses.html      # Платформенные курсы
    └── workshop.html     # Мастерская ученика
```

### Что где искать

| Задача | Файл(ы) |
|--------|---------|
| Добавить эндпоинт | `schemas.py` → `main.py` → фронт |
| Добавить таблицу/поле | `models.py` → миграция SQL → `schemas.py` → `main.py` |
| Добавить страницу | Создать HTML в `static/`, подключить `api.js`, вызвать `requireAuth()` |
| Изменить доску | `board.html` (фронт) + `main.py` (WebSocket handler) |

---

## База данных

### Основные сущности

```
users ──────────────► students
  │                      │
  │ (student_id FK)      │ 1:N
  │                      ▼
  │               student_courses (курс = группа разделов)
  │                      │
  │                      │ 1:N
  │                      ▼
  │                   sections
  │                      │
  │                      │ 1:N
  │                      ▼
  │                    items ──► attachments
  │
  └──► schedule_slots (расписание занятий)
  └──► messages       (мессенджер)
  └──► notifications
  └──► parent_student_link
```

### Таблицы

#### `users`
```
id, login (unique), password_hash, role (owner/tutor/student/parent),
name, must_change_password, student_id (FK→students, nullable),
last_seen, created_at
```

#### `students`
```
id, name, grade, goal (oge/ege/olymp/base), base_rate,
format (online/offline), created_at
```

#### `student_courses`
```
id, student_id (FK→students CASCADE), tutor_id (FK→users SET NULL),
title, created_at
```

#### `sections`
```
id, student_id (FK→students CASCADE), course_id (FK→student_courses SET NULL),
title, position, is_open, idz_enabled, idz_text, idz (1-5),
control_enabled, control (none/passed/failed)
```

#### `items`
```
id, section_id (FK→sections CASCADE), type (topic/hw/note),
position, name, status, total, done, closed, date, note,
text, closed_date, student_answer (Text)
```

#### `schedule_slots`
```
id, tutor_id (FK→users CASCADE), student_id (FK→students CASCADE, nullable),
day_of_week (0=Пн…6=Вс), slot_index (0=00:00…47=23:30),
duration (в 30-мин слотах, default=2=1ч),
note, student_note, color, created_at
```

#### `messages`
```
id, sender_id (FK→users), receiver_id (FK→users),
text, created_at, read_at
```

#### `boards`
```
id, student_id (FK→students, unique), strokes (TEXT/JSON),
created_at, updated_at
```

### ID-генерация

Все первичные ключи: `uuid.uuid4().hex[:12]` (12-символьные hex-строки).

### Перечисления

| Enum | Значения |
|------|----------|
| `UserRole` | `owner`, `tutor`, `student`, `parent` |
| `GoalType` | `oge`, `ege`, `olymp`, `base` |
| `FormatType` | `online`, `offline` |
| `ItemType` | `topic`, `hw`, `note` |
| `TopicStatus` | `none`, `progress`, `done` |
| `ControlStatus` | `none`, `passed`, `failed` |

---

## API

Base URL: `/api`

### Аутентификация

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/auth/login` | Получить JWT |
| POST | `/auth/change-password` | Сменить пароль |
| GET | `/auth/me` | Текущий пользователь |

### Пользователи

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| GET | `/users` | Список | tutor/owner |
| POST | `/users` | Создать | tutor/owner |
| DELETE | `/users/{id}` | Удалить | tutor/owner |
| POST | `/users/{uid}/link-student/{stid}` | Привязать к ученику | tutor/owner |
| DELETE | `/users/{uid}/unlink-student` | Отвязать | tutor/owner |

### Ученики

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/students` | Список (фильтр по роли) |
| GET | `/students/{id}` | Полный профиль |
| POST | `/students` | Создать |
| PATCH | `/students/{id}` | Обновить |
| DELETE | `/students/{id}` | Удалить каскадно |
| GET | `/students/{id}/contacts` | Контакты ученика |

### Курсы (StudentCourse)

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| GET | `/students/{id}/courses` | Список курсов + разделы + items | авторизованные |
| POST | `/students/{id}/courses` | Создать курс | tutor/owner |
| PATCH | `/student-courses/{cid}` | Обновить title/tutor | tutor/owner |
| DELETE | `/student-courses/{cid}` | Удалить | tutor/owner |

### Разделы и элементы

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/students/{id}/sections` | Создать раздел |
| PATCH | `/sections/{id}` | Обновить |
| DELETE | `/sections/{id}` | Удалить |
| POST | `/sections/{id}/items` | Добавить элемент |
| PATCH | `/items/{id}` | Обновить (student: только `status`, `student_answer`) |
| DELETE | `/items/{id}` | Удалить |
| POST | `/sections/{id}/items/reorder` | Переупорядочить |
| POST | `/items/{id}/attachments` | Загрузить файл |
| DELETE | `/attachments/{id}` | Удалить вложение |

### Расписание

| Метод | Путь | Описание | Доступ |
|-------|------|----------|--------|
| GET | `/schedule` | Все слоты репетитора | tutor/owner |
| POST | `/schedule` | Создать слот | tutor/owner |
| PATCH | `/schedule/{id}` | Обновить | tutor/owner |
| DELETE | `/schedule/{id}` | Удалить | tutor/owner |
| GET | `/schedule/my` | Слоты текущего ученика | student |
| PATCH | `/schedule/{id}/student-note` | Личная заметка | student |

### Мессенджер

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/contacts` | Список контактов |
| GET | `/messages/{user_id}` | История чата |
| POST | `/messages` | Отправить сообщение |
| POST | `/messages/{user_id}/read` | Отметить прочитанным |

### Доска

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/boards/{student_id}` | Данные доски |
| WS | `/ws/board/{student_id}` | WebSocket |

### Шаблоны

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/students/{id}/apply-template/{key}` | Применить шаблон |

Ключи: `oge`, `ege`, `olymp`.

---

## Авторизация и роли

| Роль | Страница | Возможности |
|------|----------|-------------|
| `owner` | `/hbm_tutor.html` | Полный доступ, владелец аккаунта |
| `tutor` | `/hbm_tutor.html` | Управление учениками, расписанием, курсами |
| `student` | `/student_lk.html` | Просмотр своего профиля, расписания, ответы на ДЗ |
| `parent` | `/parent_lk.html` | Просмотр профилей привязанных детей |

Онлайн-статус: `last_seen` обновляется middleware на каждый запрос. Онлайн = ≤ 5 минут назад.

---

## Фронтенд — страницы

### api.js — ВАЖНО

```js
// Объявлены в глобальном скопе — нельзя переобъявлять в inline-скриптах!
const get, post, patch, del
```

Если нужны кастомные хелперы в HTML — называть `apiPost`, `apiDelete` и т.д.

### hbm_tutor.html (ЛК репетитора)

**Табы:** Главная | Расписание | Пользователи | Сообщения

- Карточки учеников с прогрессом и тарифом
- Расписание: drag-to-select, шторки, rowspan
- Пользователи: привязка user↔student
- Мессенджер: polling 5 сек

### student_lk.html (ЛК ученика)

**Разделы:** Главная | Основные курсы | Расписание | Дополнительные | Контакты

- Расписание: read-only, клик → доска, личные заметки
- Курсы: StudentCourse → разделы → items с textarea для ответов на ДЗ
- Контакты: перезагрузка при открытии

### parent_lk.html / profile.html

- parent: список детей → профиль; мессенджер
- profile: кнопка "Написать" → `?chat=uid&name=name`

---

## Расписание

**Слоты:** 48 слотов × 30 мин. `slot_index = час * 2 + (мин==30 ? 1 : 0)`.

Примеры: 13:00 → 26, 16:00 → 32, 17:30 → 35, 19:00 → 38, 20:00 → 40.

**Диапазон по умолчанию:** 10:00–21:00 (`SCHED_VIS_START=20`, `SCHED_VIS_END=41`).

**Добавление:**
- Одиночный клик → модал, длительность 1ч по умолчанию
- Drag через ячейки → длительность = число выбранных слотов
- Клик по чипу занятия → `/board.html?id=SID`

---

## Мессенджер

- Polling `GET /api/messages/{uid}` каждые 5 сек при открытом чате
- `setInterval` стартует в `openChatWith()`, `clearInterval` при закрытии
- Уведомления: `GET /api/notifications` (отдельный polling)

---

## Курсы и программа

```
StudentCourse ("ОГЭ Математика с Иваном")
  └─ Section ("Алгебра")
       └─ Item (topic / hw / note)
            └─ Attachment
```

| Тип | Статусы | Ответ ученика |
|-----|---------|---------------|
| `topic` | none → progress → done | — |
| `hw` | pending → closed (done/total) | `student_answer` textarea |
| `note` | — | — |

**Формула прогресса:**
```
score(section) = среднее(hw.done/hw.total, idz/5, control=='passed')
progress = среднее(score по разделам) × 100%
```

**Формула тарифа:**
```
discount = floor(progress% × 0.3)
rate = base_rate × (1 − discount/100)
```

---

## Электронная доска

URL: `/board.html?id=STUDENT_ID`

**Топбар (52px):** HBM | Имя ученика | ↩ Undo | 🗑 Очистить | 🌙 Тема | ⊙ Сброс | ← Назад

### Инструменты

| Клавиша | Действие |
|---------|----------|
| V | Выделение (move/resize/rotate) |
| P | Перо |
| E | Ластик |
| G | Геометрия (линия/прямоугольник/эллипс) |
| T | Текст |
| I | Изображение |
| Ctrl+Z | Undo (локальный стек 50 шагов) |
| Del | Удалить выделенное |
| ESC | Сбросить выделение |

### Система координат (v3.6)

- Выделить 2 линии → ⊕ Создать СК
- Настройка масштаба осей, ввод уравнений `y=f(x)` / `x=f(y)`
- Перетаскивание оси одним кликом (наведи на конец → crosshair)
- Копирование графика: 📋 → монолитный stroke

### WebSocket

```
hello / load / strokes / stroke / erase_stroke / undo / clear
```

---

## Развёртывание

```bash
# Переменные окружения (продакшен)
HBM_JWT_SECRET=<32+ символа>
DATABASE_URL=postgresql://user:pass@host:5432/hbm

# Запуск (workers=1 — WebSocket state in-memory)
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

**Зависимости:** `fastapi`, `uvicorn[standard]`, `sqlalchemy`, `psycopg2-binary`, `pyjwt`, `bcrypt`, `python-multipart`

---

*Проприетарный проект. Все права защищены.*
