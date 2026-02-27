# ╔══════════════════════════════════════════════════════════════╗
# ║  HBM РЕПЕТИТОР — models.py v1.1                             ║
# ║  + users, parent_student_link                                ║
# ╚══════════════════════════════════════════════════════════════╝

from sqlalchemy import (
    Column, String, Integer, Boolean, Text,
    DateTime, ForeignKey, Enum as PgEnum, Table
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum, uuid

from database import Base

# ─── Перечисления ───

class GoalType(str, enum.Enum):
    ege = "ege"; olymp = "olymp"; base = "base"

class FormatType(str, enum.Enum):
    online = "online"; offline = "offline"

class ItemType(str, enum.Enum):
    topic = "topic"; hw = "hw"; note = "note"

class TopicStatus(str, enum.Enum):
    none = "none"; progress = "progress"; done = "done"

class ControlStatus(str, enum.Enum):
    none = "none"; passed = "passed"; failed = "failed"

class UserRole(str, enum.Enum):
    tutor = "tutor"; parent = "parent"; student = "student"

def gen_id():
    return uuid.uuid4().hex[:12]

# ── Связь родитель ↔ ученик (many-to-many) ──
parent_student_link = Table(
    "parent_student_link", Base.metadata,
    Column("parent_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("student_id", String(12), ForeignKey("students.id", ondelete="CASCADE"), primary_key=True),
)

class User(Base):
    __tablename__ = "users"
    id            = Column(String(12), primary_key=True, default=gen_id)
    login         = Column(String(100), unique=True, nullable=False)
    password_hash = Column(String(200), nullable=False)
    role          = Column(PgEnum(UserRole), nullable=False)
    name          = Column(String(200), nullable=False)
    must_change_password = Column(Boolean, default=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
    # Привязка user(student) → student profile
    student_id    = Column(String(12), ForeignKey("students.id", ondelete="SET NULL"), nullable=True)
    student_profile = relationship("Student", foreign_keys=[student_id])

class Student(Base):
    __tablename__ = "students"
    id         = Column(String(12), primary_key=True, default=gen_id)
    name       = Column(String(200), nullable=False)
    grade      = Column(String(20), nullable=False, default="9")
    goal       = Column(PgEnum(GoalType), nullable=False, default=GoalType.ege)
    base_rate  = Column(Integer, nullable=False, default=1500)
    format     = Column(PgEnum(FormatType), nullable=False, default=FormatType.online)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    sections   = relationship("Section", back_populates="student",
                              cascade="all, delete-orphan", order_by="Section.position")
    parents    = relationship("User", secondary=parent_student_link, backref="children_students")

class Section(Base):
    __tablename__ = "sections"
    id              = Column(String(12), primary_key=True, default=gen_id)
    student_id      = Column(String(12), ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    title           = Column(String(300), nullable=False)
    position        = Column(Integer, nullable=False, default=0)
    is_open         = Column(Boolean, default=False)
    idz_enabled     = Column(Boolean, default=True)
    control_enabled = Column(Boolean, default=True)
    idz             = Column(Integer, default=0)
    control         = Column(PgEnum(ControlStatus), default=ControlStatus.none)
    student = relationship("Student", back_populates="sections")
    items   = relationship("Item", back_populates="section",
                           cascade="all, delete-orphan", order_by="Item.position")

class Item(Base):
    __tablename__ = "items"
    id          = Column(String(12), primary_key=True, default=gen_id)
    section_id  = Column(String(12), ForeignKey("sections.id", ondelete="CASCADE"), nullable=False)
    type        = Column(PgEnum(ItemType), nullable=False)
    position    = Column(Integer, nullable=False, default=0)
    name        = Column(String(300))
    status      = Column(PgEnum(TopicStatus), default=TopicStatus.none)
    total       = Column(Integer)
    done        = Column(Integer)
    closed      = Column(Boolean, default=False)
    date        = Column(String(20))
    closed_date = Column(String(20))
    note        = Column(Text)
    text        = Column(Text)
    section     = relationship("Section", back_populates="items")
    attachments = relationship("Attachment", back_populates="item", cascade="all, delete-orphan")

class Attachment(Base):
    __tablename__ = "attachments"
    id        = Column(String(12), primary_key=True, default=gen_id)
    item_id   = Column(String(12), ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    name      = Column(String(500), nullable=False)
    mime      = Column(String(200), nullable=False)
    size      = Column(Integer, nullable=False)
    file_path = Column(String(1000))
    item      = relationship("Item", back_populates="attachments")

class Board(Base):
    __tablename__ = "boards"
    id         = Column(String(12), primary_key=True, default=gen_id)
    student_id = Column(String(12), ForeignKey("students.id", ondelete="CASCADE"), nullable=False, unique=True)
    strokes    = Column(Text, default="[]")          # JSON-массив штрихов
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    student    = relationship("Student", backref="board")
