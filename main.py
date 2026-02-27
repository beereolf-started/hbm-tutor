# ╔══════════════════════════════════════════════════════════════╗
# ║  HBM РЕПЕТИТОР — main.py v1.1                               ║
# ║  + авторизация, роли, управление пользователями              ║
# ╚══════════════════════════════════════════════════════════════╝

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload
import os, json, jwt
from collections import defaultdict

from database import get_db, SessionLocal
from models import (
    Student, Section, Item, Attachment, User, Board,
    parent_student_link, gen_id
)
from schemas import *
from auth import (
    hash_password, verify_password, create_token,
    get_current_user, require_tutor, decode_token
)

app = FastAPI(title="HBM Репетитор API", version="1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ══════════════════════════════════════════════════════════════
#  AUTH — логин, смена пароля, текущий пользователь
# ══════════════════════════════════════════════════════════════

@app.post("/api/auth/login", response_model=LoginResponse, summary="Войти")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.login == data.login).first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Неверный логин или пароль")
    token = create_token(user.id, user.role)
    return LoginResponse(
        token=token,
        role=user.role,
        name=user.name,
        must_change_password=user.must_change_password,
    )


@app.post("/api/auth/change-password", summary="Сменить пароль")
def change_password(data: ChangePasswordRequest,
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(400, "Неверный текущий пароль")
    if len(data.new_password) < 6:
        raise HTTPException(400, "Пароль должен быть не менее 6 символов")
    user.password_hash = hash_password(data.new_password)
    user.must_change_password = False
    db.commit()
    return {"ok": True}


@app.get("/api/auth/me", response_model=UserOut, summary="Текущий пользователь")
def get_me(user: User = Depends(get_current_user)):
    return user


# ══════════════════════════════════════════════════════════════
#  USERS — управление аккаунтами (только репетитор)
# ══════════════════════════════════════════════════════════════

@app.get("/api/users", response_model=list[UserOut],
         summary="Список всех пользователей (только репетитор)")
def list_users(tutor: User = Depends(require_tutor),
               db: Session = Depends(get_db)):
    return db.query(User).order_by(User.created_at.desc()).all()


@app.post("/api/users", response_model=UserOut, status_code=201,
          summary="Создать аккаунт ученика или родителя (только репетитор)")
def create_user(data: UserCreate,
                tutor: User = Depends(require_tutor),
                db: Session = Depends(get_db)):
    if data.role not in ("student", "parent"):
        raise HTTPException(400, "Можно создать только student или parent")
    if db.query(User).filter(User.login == data.login).first():
        raise HTTPException(409, "Логин уже занят")
    if len(data.password) < 6:
        raise HTTPException(400, "Пароль должен быть не менее 6 символов")

    user = User(
        login=data.login,
        password_hash=hash_password(data.password),
        role=data.role,
        name=data.name,
        must_change_password=True,
    )

    # Привязка student → student profile
    if data.role == "student" and data.student_id:
        student = db.query(Student).filter(Student.id == data.student_id).first()
        if not student:
            raise HTTPException(404, "Ученик не найден")
        user.student_id = data.student_id

    db.add(user)
    db.flush()

    # Привязка parent → children
    if data.role == "parent" and data.children_ids:
        for sid in data.children_ids:
            student = db.query(Student).filter(Student.id == sid).first()
            if student:
                db.execute(parent_student_link.insert().values(
                    parent_id=user.id, student_id=sid
                ))

    db.commit()
    db.refresh(user)
    return user


@app.delete("/api/users/{user_id}", status_code=204,
            summary="Удалить аккаунт (только репетитор)")
def delete_user(user_id: str,
                tutor: User = Depends(require_tutor),
                db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.role == "tutor":
        raise HTTPException(400, "Нельзя удалить аккаунт репетитора")
    db.delete(user)
    db.commit()


# ══════════════════════════════════════════════════════════════
#  ХЕЛПЕР: проверка доступа к ученику
# ══════════════════════════════════════════════════════════════

def check_student_access(student_id: str, user: User, db: Session):
    """Проверяет, имеет ли пользователь доступ к данному ученику."""
    if user.role == "tutor":
        return  # полный доступ
    if user.role == "student":
        if user.student_id != student_id:
            raise HTTPException(403, "Нет доступа к этому ученику")
    elif user.role == "parent":
        link = db.execute(
            parent_student_link.select().where(
                (parent_student_link.c.parent_id == user.id) &
                (parent_student_link.c.student_id == student_id)
            )
        ).first()
        if not link:
            raise HTTPException(403, "Нет доступа к этому ученику")


# ══════════════════════════════════════════════════════════════
#  STUDENTS — ученики
# ══════════════════════════════════════════════════════════════

@app.get("/api/students", response_model=list[StudentListItem],
         summary="Список учеников")
def list_students(user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    if user.role == "tutor":
        return db.query(Student).order_by(Student.created_at.desc()).all()
    elif user.role == "student":
        if user.student_id:
            s = db.query(Student).filter(Student.id == user.student_id).first()
            return [s] if s else []
        return []
    elif user.role == "parent":
        # Все дети этого родителя
        rows = db.execute(
            parent_student_link.select().where(
                parent_student_link.c.parent_id == user.id
            )
        ).fetchall()
        ids = [r.student_id for r in rows]
        if not ids:
            return []
        return db.query(Student).filter(Student.id.in_(ids)).all()
    return []


@app.get("/api/students/{student_id}", response_model=StudentOut,
         summary="Получить ученика со всеми разделами")
def get_student(student_id: str,
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    check_student_access(student_id, user, db)
    student = (
        db.query(Student)
        .options(
            joinedload(Student.sections)
            .joinedload(Section.items)
            .joinedload(Item.attachments)
        )
        .filter(Student.id == student_id)
        .first()
    )
    if not student:
        raise HTTPException(404, "Ученик не найден")
    return student


@app.post("/api/students", response_model=StudentOut, status_code=201,
          summary="Добавить ученика (только репетитор)")
def create_student(data: StudentCreate,
                   tutor: User = Depends(require_tutor),
                   db: Session = Depends(get_db)):
    student = Student(
        name=data.name, grade=data.grade, goal=data.goal,
        base_rate=data.base_rate, format=data.format,
    )
    db.add(student)
    db.commit()
    db.refresh(student)
    return student


@app.patch("/api/students/{student_id}", response_model=StudentOut,
           summary="Обновить ученика (только репетитор)")
def update_student(student_id: str, data: StudentUpdate,
                   tutor: User = Depends(require_tutor),
                   db: Session = Depends(get_db)):
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(404, "Ученик не найден")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(student, field, value)
    db.commit()
    db.refresh(student)
    return student


@app.delete("/api/students/{student_id}", status_code=204,
            summary="Удалить ученика (только репетитор)")
def delete_student(student_id: str,
                   tutor: User = Depends(require_tutor),
                   db: Session = Depends(get_db)):
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(404, "Ученик не найден")
    _cleanup_student_files(student, db)
    db.delete(student)
    db.commit()


# ══════════════════════════════════════════════════════════════
#  SECTIONS (только репетитор может редактировать)
# ══════════════════════════════════════════════════════════════

@app.post("/api/students/{student_id}/sections", response_model=SectionOut,
          status_code=201, summary="Добавить раздел")
def create_section(student_id: str, data: SectionCreate,
                   tutor: User = Depends(require_tutor),
                   db: Session = Depends(get_db)):
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(404, "Ученик не найден")
    max_pos = db.query(Section.position).filter(
        Section.student_id == student_id
    ).order_by(Section.position.desc()).first()
    position = (max_pos[0] + 1) if max_pos else 0
    section = Section(
        student_id=student_id, title=data.title, position=position,
        idz_enabled=data.idz_enabled, control_enabled=data.control_enabled,
    )
    db.add(section)
    db.commit()
    db.refresh(section)
    return section


@app.patch("/api/sections/{section_id}", response_model=SectionOut,
           summary="Обновить раздел")
def update_section(section_id: str, data: SectionUpdate,
                   user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    section = db.query(Section).filter(Section.id == section_id).first()
    if not section:
        raise HTTPException(404, "Раздел не найден")
    # is_open может менять кто угодно (UI-состояние), остальное — только репетитор
    update_data = data.model_dump(exclude_unset=True)
    if user.role != "tutor":
        allowed = {"is_open"}
        if set(update_data.keys()) - allowed:
            raise HTTPException(403, "Нет прав на редактирование")
    for field, value in update_data.items():
        setattr(section, field, value)
    db.commit()
    db.refresh(section)
    return section


@app.delete("/api/sections/{section_id}", status_code=204,
            summary="Удалить раздел")
def delete_section(section_id: str,
                   tutor: User = Depends(require_tutor),
                   db: Session = Depends(get_db)):
    section = db.query(Section).filter(Section.id == section_id).first()
    if not section:
        raise HTTPException(404, "Раздел не найден")
    _cleanup_section_files(section, db)
    db.delete(section)
    db.commit()


# ══════════════════════════════════════════════════════════════
#  ITEMS (только репетитор может редактировать)
# ══════════════════════════════════════════════════════════════

@app.post("/api/sections/{section_id}/items", response_model=ItemOut,
          status_code=201, summary="Добавить элемент в ленту")
def create_item(section_id: str, data: ItemCreate,
                tutor: User = Depends(require_tutor),
                db: Session = Depends(get_db)):
    section = db.query(Section).filter(Section.id == section_id).first()
    if not section:
        raise HTTPException(404, "Раздел не найден")
    max_pos = db.query(Item.position).filter(
        Item.section_id == section_id
    ).order_by(Item.position.desc()).first()
    position = (max_pos[0] + 1) if max_pos else 0
    item = Item(
        section_id=section_id, type=data.type, position=position,
        name=data.name, status=data.status or "none",
        total=data.total, done=data.done, closed=data.closed or False,
        date=data.date, closed_date=data.closed_date, note=data.note, text=data.text,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@app.patch("/api/items/{item_id}", response_model=ItemOut,
           summary="Обновить элемент")
def update_item(item_id: str, data: ItemUpdate,
                tutor: User = Depends(require_tutor),
                db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(404, "Элемент не найден")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return item


@app.delete("/api/items/{item_id}", status_code=204,
            summary="Удалить элемент")
def delete_item(item_id: str,
                tutor: User = Depends(require_tutor),
                db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(404, "Элемент не найден")
    if item.type == "hw":
        raise HTTPException(400, "ДЗ нельзя удалить")
    _cleanup_item_files(item, db)
    db.delete(item)
    db.commit()


@app.post("/api/sections/{section_id}/items/reorder", status_code=200,
          summary="Переупорядочить элементы")
def reorder_items(section_id: str, item_ids: list[str],
                  tutor: User = Depends(require_tutor),
                  db: Session = Depends(get_db)):
    items = db.query(Item).filter(Item.section_id == section_id).all()
    id_to_item = {i.id: i for i in items}
    for pos, iid in enumerate(item_ids):
        if iid in id_to_item:
            id_to_item[iid].position = pos
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
#  ATTACHMENTS
# ══════════════════════════════════════════════════════════════

@app.post("/api/items/{item_id}/attachments", response_model=AttachmentOut,
          status_code=201, summary="Загрузить файл")
async def upload_attachment(item_id: str, file: UploadFile = File(...),
                            tutor: User = Depends(require_tutor),
                            db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(404, "Элемент не найден")
    att_id = gen_id()
    ext = os.path.splitext(file.filename)[1] if file.filename else ""
    file_path = os.path.join(UPLOAD_DIR, f"{att_id}{ext}")
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(413, "Файл больше 50 МБ")
    with open(file_path, "wb") as f:
        f.write(content)
    attachment = Attachment(
        id=att_id, item_id=item_id, name=file.filename or "file",
        mime=file.content_type or "application/octet-stream",
        size=len(content), file_path=file_path,
    )
    db.add(attachment)
    db.commit()
    db.refresh(attachment)
    return attachment


@app.delete("/api/attachments/{att_id}", status_code=204,
            summary="Удалить вложение")
def delete_attachment(att_id: str,
                      tutor: User = Depends(require_tutor),
                      db: Session = Depends(get_db)):
    att = db.query(Attachment).filter(Attachment.id == att_id).first()
    if not att:
        raise HTTPException(404, "Вложение не найдено")
    if att.file_path and os.path.exists(att.file_path):
        os.remove(att.file_path)
    db.delete(att)
    db.commit()


# ══════════════════════════════════════════════════════════════
#  ШАБЛОНЫ
# ══════════════════════════════════════════════════════════════

TEMPLATES = {
    "oge": [
        {"title": "Алгебра. База", "idz": True, "control": True, "items": [
            "Числа и вычисления", "Степени и корни", "Уравнения и системы",
            "Неравенства", "Функции и графики", "Прогрессии"]},
        {"title": "Геометрия. Планиметрия", "idz": True, "control": True, "items": [
            "Треугольники", "Четырёхугольники", "Окружность и круг",
            "Подобие фигур", "Площади"]},
        {"title": "Статистика и вероятность", "idz": True, "control": True, "items": [
            "Таблицы и графики", "Теория вероятностей"]},
        {"title": "Задачи повышенной сложности (2 часть)", "idz": False, "control": False, "items": [
            "Задача 19 — алгебра", "Задача 20 — геометрия", "Задача 21 — доказательство"]},
    ],
    "ege": [
        {"title": "Алгебра и начала анализа", "idz": True, "control": True, "items": [
            "Тригонометрия", "Показательные и логарифмические функции",
            "Производная", "Первообразная и интеграл", "Уравнения", "Неравенства"]},
        {"title": "Геометрия", "idz": True, "control": True, "items": [
            "Планиметрия", "Стереометрия"]},
        {"title": "Статистика и вероятность", "idz": True, "control": True, "items": [
            "Комбинаторика", "Вероятность", "Статистика"]},
        {"title": "Профильные задачи (часть 2)", "idz": True, "control": False, "items": [
            "Задача на уравнение", "Задача на неравенство",
            "Задача на геометрию", "Задача с параметром", "Задача на доказательство"]},
    ],
    "olymp": [
        {"title": "Алгебра", "idz": True, "control": True, "items": [
            "Тождества и преобразования", "Диофантовы уравнения",
            "Функциональные уравнения", "Неравенства (AM-GM, КБШ)"]},
        {"title": "Комбинаторика и теория чисел", "idz": True, "control": True, "items": [
            "Делимость и остатки", "НОД и НОК",
            "Принцип Дирихле", "Инвариант и полуинвариант"]},
        {"title": "Геометрия", "idz": True, "control": True, "items": [
            "Вписанные углы и хорды", "Радикальная ось", "Аффинные преобразования"]},
        {"title": "Задачи по уровням", "idz": True, "control": False, "items": [
            "Муниципальный тур", "Региональный тур", "Всероссийский тур"]},
    ],
}

@app.post("/api/students/{student_id}/apply-template/{template_key}",
          response_model=StudentOut,
          summary="Применить шаблон (только репетитор)")
def apply_template(student_id: str, template_key: str,
                   tutor: User = Depends(require_tutor),
                   db: Session = Depends(get_db)):
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(404, "Ученик не найден")
    if template_key not in TEMPLATES:
        raise HTTPException(400, f"Неизвестный шаблон: {template_key}")
    for sec in student.sections:
        _cleanup_section_files(sec, db)
    db.query(Section).filter(Section.student_id == student_id).delete()
    for pos, tmpl_sec in enumerate(TEMPLATES[template_key]):
        section = Section(
            student_id=student_id, title=tmpl_sec["title"], position=pos,
            idz_enabled=tmpl_sec["idz"], control_enabled=tmpl_sec["control"],
        )
        db.add(section)
        db.flush()
        for item_pos, topic_name in enumerate(tmpl_sec["items"]):
            db.add(Item(
                section_id=section.id, type="topic", position=item_pos,
                name=topic_name, status="none",
            ))
    db.commit()
    db.refresh(student)
    return get_student(student_id, tutor, db)


# ══════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ
# ══════════════════════════════════════════════════════════════

def _cleanup_item_files(item, db):
    for att in item.attachments:
        if att.file_path and os.path.exists(att.file_path):
            os.remove(att.file_path)

def _cleanup_section_files(section, db):
    for item in db.query(Item).filter(Item.section_id == section.id).all():
        _cleanup_item_files(item, db)

def _cleanup_student_files(student, db):
    for sec in db.query(Section).filter(Section.student_id == student.id).all():
        _cleanup_section_files(sec, db)


# ══════════════════════════════════════════════════════════════
#  BOARD — электронная доска (WebSocket real-time)
# ══════════════════════════════════════════════════════════════

# Активные WS-соединения: student_id → set[WebSocket]
board_connections: dict[str, set[WebSocket]] = defaultdict(set)


def _get_or_create_board(student_id: str, db: Session) -> Board:
    """Получить или создать доску для ученика."""
    board = db.query(Board).filter(Board.student_id == student_id).first()
    if not board:
        board = Board(student_id=student_id, strokes="[]")
        db.add(board)
        db.commit()
        db.refresh(board)
    return board


@app.get("/api/boards/{student_id}", response_model=BoardOut,
         summary="Получить доску ученика")
def get_board(student_id: str,
              user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    check_student_access(student_id, user, db)
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(404, "Ученик не найден")
    return _get_or_create_board(student_id, db)


@app.post("/api/boards/{student_id}/clear",
          summary="Очистить доску (только репетитор)")
def clear_board(student_id: str,
                tutor: User = Depends(require_tutor),
                db: Session = Depends(get_db)):
    board = db.query(Board).filter(Board.student_id == student_id).first()
    if board:
        board.strokes = "[]"
        db.commit()
    return {"ok": True}


@app.websocket("/ws/board/{student_id}")
async def board_ws(ws: WebSocket, student_id: str):
    """WebSocket для совместной доски."""
    await ws.accept()

    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=4001, reason="Требуется токен")
        return
    try:
        payload = jwt.decode(token, "hbm-secret-change-me-in-production", algorithms=["HS256"])
    except Exception:
        await ws.close(code=4001, reason="Невалидный токен")
        return

    user_id = payload.get("sub")
    user_role = payload.get("role")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            await ws.close(code=4001, reason="Пользователь не найден")
            return
        if user.role == "student" and user.student_id != student_id:
            await ws.close(code=4003, reason="Нет доступа")
            return
        elif user.role == "parent":
            link = db.execute(
                parent_student_link.select().where(
                    (parent_student_link.c.parent_id == user.id) &
                    (parent_student_link.c.student_id == student_id)
                )
            ).first()
            if not link:
                await ws.close(code=4003, reason="Нет доступа")
                return
        _get_or_create_board(student_id, db)
    finally:
        db.close()

    board_connections[student_id].add(ws)
    # Отправляем user_id клиенту чтобы он знал свой id
    await ws.send_text(json.dumps({"type": "hello", "user_id": user_id}))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "load":
                db = SessionLocal()
                try:
                    db.expire_all()
                    board = db.query(Board).filter(Board.student_id == student_id).first()
                    strokes_json = board.strokes if board else "[]"
                finally:
                    db.close()
                await ws.send_text(json.dumps({"type": "strokes", "data": json.loads(strokes_json)}))

            elif msg_type == "stroke":
                stroke_data = msg.get("data", {})
                # Если объект уже имеет user_id (редактирование чужого) — сохраняем его
                # Иначе ставим автора
                if "user_id" not in stroke_data or not stroke_data["user_id"]:
                    stroke_data["user_id"] = user_id
                db = SessionLocal()
                try:
                    board = _get_or_create_board(student_id, db)
                    current = json.loads(board.strokes)
                    current.append(stroke_data)
                    board.strokes = json.dumps(current)
                    db.commit()
                finally:
                    db.close()
                await _broadcast(student_id, json.dumps({"type": "stroke", "data": stroke_data}), ws)

            elif msg_type == "clear":
                if user_role != "tutor":
                    continue
                db = SessionLocal()
                try:
                    board = db.query(Board).filter(Board.student_id == student_id).first()
                    if board:
                        board.strokes = "[]"
                        db.commit()
                finally:
                    db.close()
                await _broadcast(student_id, json.dumps({"type": "clear"}), ws)

            elif msg_type == "undo":
                # Удалить последний штрих ЭТОГО пользователя
                print(f"[WS board] UNDO request from user_id={user_id}")
                db = SessionLocal()
                try:
                    board = _get_or_create_board(student_id, db)
                    current = json.loads(board.strokes)
                    print(f"[WS board] Total objects: {len(current)}, user_ids: {[s.get('user_id') for s in current[-5:]]}")
                    # Найти последний штрих этого юзера
                    removed_id = None
                    for i in range(len(current) - 1, -1, -1):
                        if current[i].get("user_id") == user_id:
                            removed_id = current[i].get("id")
                            current.pop(i)
                            print(f"[WS board] Removing object id={removed_id}")
                            break
                    if removed_id:
                        board.strokes = json.dumps(current)
                        db.commit()
                    else:
                        print(f"[WS board] No objects found for user_id={user_id}")
                finally:
                    db.close()
                if removed_id:
                    await ws.send_text(json.dumps({"type": "erase_stroke", "id": removed_id}))
                    await _broadcast(student_id, json.dumps({"type": "erase_stroke", "id": removed_id}), ws)

            elif msg_type == "erase_stroke":
                stroke_id = msg.get("id")
                if not stroke_id:
                    continue
                db = SessionLocal()
                try:
                    board = _get_or_create_board(student_id, db)
                    current = json.loads(board.strokes)
                    board.strokes = json.dumps([s for s in current if s.get("id") != stroke_id])
                    db.commit()
                finally:
                    db.close()
                await _broadcast(student_id, json.dumps({"type": "erase_stroke", "id": stroke_id}), ws)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS board] Ошибка: {e}")
    finally:
        board_connections[student_id].discard(ws)
        if not board_connections[student_id]:
            del board_connections[student_id]


async def _broadcast(student_id: str, message: str, sender: WebSocket):
    """Разослать всем подключённым к доске кроме отправителя."""
    dead = set()
    for conn in board_connections.get(student_id, set()):
        if conn is not sender:
            try:
                await conn.send_text(message)
            except Exception:
                dead.add(conn)
    if dead and student_id in board_connections:
        board_connections[student_id] -= dead


# ── Раздача файлов ──
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# Статику раздаём НЕ через catch-all mount (он блокирует WebSocket)
STATIC_DIR = "static"
if os.path.isdir(STATIC_DIR):
    from fastapi.responses import FileResponse, HTMLResponse

    # Раздача api.js и других статических файлов
    @app.get("/api.js")
    async def serve_api_js():
        return FileResponse(os.path.join(STATIC_DIR, "api.js"), media_type="application/javascript")

    @app.get("/{page}.html")
    async def serve_html(page: str):
        fpath = os.path.join(STATIC_DIR, f"{page}.html")
        if os.path.isfile(fpath):
            return FileResponse(fpath, media_type="text/html")
        raise HTTPException(404)

    @app.get("/")
    async def serve_index():
        # По умолчанию — login
        fpath = os.path.join(STATIC_DIR, "login.html")
        if os.path.isfile(fpath):
            return FileResponse(fpath, media_type="text/html")
        return HTMLResponse("HBM Репетитор")
