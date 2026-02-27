# ╔══════════════════════════════════════════════════════════════╗
# ║  HBM РЕПЕТИТОР — schemas.py v1.1                            ║
# ║  + схемы для auth, users                                     ║
# ╚══════════════════════════════════════════════════════════════╝

from pydantic import BaseModel
from typing import Optional
from datetime import datetime

# ── Auth ──

class LoginRequest(BaseModel):
    login: str
    password: str

class LoginResponse(BaseModel):
    token: str
    role: str
    name: str
    must_change_password: bool

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

# ── User (создание репетитором) ──

class UserCreate(BaseModel):
    login: str
    password: str                    # временный пароль
    role: str                        # "student" | "parent"
    name: str
    student_id: Optional[str] = None # для role=student — привязка к профилю
    children_ids: list[str] = []     # для role=parent — привязка к ученикам

class UserOut(BaseModel):
    id: str
    login: str
    role: str
    name: str
    must_change_password: bool
    student_id: Optional[str] = None
    created_at: Optional[datetime] = None
    model_config = {"from_attributes": True}

# ── Attachment ──

class AttachmentOut(BaseModel):
    id: str
    item_id: str
    name: str
    mime: str
    size: int
    file_path: Optional[str] = None
    model_config = {"from_attributes": True}

# ── Item ──

class ItemCreate(BaseModel):
    type: str
    name: Optional[str] = None
    status: Optional[str] = "none"
    total: Optional[int] = None
    done: Optional[int] = None
    closed: Optional[bool] = False
    date: Optional[str] = None
    closed_date: Optional[str] = None
    note: Optional[str] = None
    text: Optional[str] = None

class ItemUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    total: Optional[int] = None
    done: Optional[int] = None
    closed: Optional[bool] = None
    closed_date: Optional[str] = None
    note: Optional[str] = None
    text: Optional[str] = None

class ItemOut(BaseModel):
    id: str
    section_id: str
    type: str
    position: int
    name: Optional[str] = None
    status: Optional[str] = None
    total: Optional[int] = None
    done: Optional[int] = None
    closed: Optional[bool] = None
    date: Optional[str] = None
    closed_date: Optional[str] = None
    note: Optional[str] = None
    text: Optional[str] = None
    attachments: list[AttachmentOut] = []
    model_config = {"from_attributes": True}

# ── Section ──

class SectionCreate(BaseModel):
    title: str
    idz_enabled: bool = True
    control_enabled: bool = True

class SectionUpdate(BaseModel):
    title: Optional[str] = None
    is_open: Optional[bool] = None
    idz_enabled: Optional[bool] = None
    control_enabled: Optional[bool] = None
    idz: Optional[int] = None
    control: Optional[str] = None

class SectionOut(BaseModel):
    id: str
    student_id: str
    title: str
    position: int
    is_open: bool
    idz_enabled: bool
    control_enabled: bool
    idz: int
    control: str
    items: list[ItemOut] = []
    model_config = {"from_attributes": True}

# ── Student ──

class StudentCreate(BaseModel):
    name: str
    grade: str = "9"
    goal: str = "ege"
    base_rate: int = 1500
    format: str = "online"

class StudentUpdate(BaseModel):
    name: Optional[str] = None
    grade: Optional[str] = None
    goal: Optional[str] = None
    base_rate: Optional[int] = None
    format: Optional[str] = None

class StudentOut(BaseModel):
    id: str
    name: str
    grade: str
    goal: str
    base_rate: int
    format: str
    created_at: Optional[datetime] = None
    sections: list[SectionOut] = []
    model_config = {"from_attributes": True}

class StudentListItem(BaseModel):
    id: str
    name: str
    grade: str
    goal: str
    base_rate: int
    format: str
    created_at: Optional[datetime] = None
    model_config = {"from_attributes": True}

# ── Board ──

class BoardOut(BaseModel):
    id: str
    student_id: str
    strokes: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    model_config = {"from_attributes": True}
