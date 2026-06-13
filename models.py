from sqlalchemy import (Column, String, Integer, Boolean, Text, DateTime, ForeignKey, Enum as PgEnum, Table)

from sqlalchemy.orm import relationship

from sqlalchemy.sql import func

import enum, uuid

from database import Base



class GoalType(str, enum.Enum):

    ege="ege"; oge="oge"; olymp="olymp"

    improve_grades="improve_grades"; deepening="deepening"; extra_education="extra_education"

class StudentLevelType(str, enum.Enum):

    school="school"; university="university"; additional="additional"

class FormatType(str, enum.Enum):

    online="online"; offline="offline"

class ItemType(str, enum.Enum):

    topic="topic"; hw="hw"; note="note"; media="media"; control="control"; idz="idz"; code="code"

class TopicStatus(str, enum.Enum):

    none="none"; progress="progress"; done="done"

class ControlStatus(str, enum.Enum):

    none="none"; passed="passed"; failed="failed"

class UserRole(str, enum.Enum):

    owner="owner"; tutor="tutor"; parent="parent"; student="student"; board_user="board_user"

    teamlead="teamlead"; recruiter="recruiter"; demo_tutor="demo_tutor"; demo_teamlead="demo_teamlead"

class SubscriptionModel(str, enum.Enum):

    percent="percent"; fixed="fixed"; none="none"

class PaymentModel(str, enum.Enum):

    centralized="centralized"; decentralized="decentralized"

class SubscriptionPaymentStatus(str, enum.Enum):

    pending="pending"; confirmed="confirmed"

class LessonStatus(str, enum.Enum):

    conducted="conducted"; cancelled="cancelled"; rescheduled="rescheduled"

class PaymentStatus(str, enum.Enum):

    unpaid="unpaid"; paid="paid"; disputed="disputed"

class PayerModel(str, enum.Enum):

    self="self"; parent="parent"

class CourseAccess(str, enum.Enum):

    public="public"; internal="internal"; private="private"

class StudyPlanStatus(str, enum.Enum):

    active="active"; paused="paused"; done="done"

class SubscriptionStatus(str, enum.Enum):

    unverified="unverified"; trial="trial"; pending_approval="pending_approval"

    active="active"; pending_payment="pending_payment"; expired="expired"



def gen_id(): return uuid.uuid4().hex[:12]



parent_student_link = Table("parent_student_link", Base.metadata,

    Column("parent_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),

    Column("student_id", String(12), ForeignKey("students.id", ondelete="CASCADE"), primary_key=True))

tutor_student_link = Table("tutor_student_link", Base.metadata,

    Column("tutor_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),

    Column("student_id", String(12), ForeignKey("students.id", ondelete="CASCADE"), primary_key=True))

tutor_subject_link = Table("tutor_subjects", Base.metadata,

    Column("tutor_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),

    Column("subject_id", String(12), ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True))



class User(Base):

    __tablename__="users"

    id=Column(String(12),primary_key=True,default=gen_id); login=Column(String(100),unique=True,nullable=False)

    password_hash=Column(String(200),nullable=False); role=Column(PgEnum(UserRole),nullable=False)

    name=Column(String(200),nullable=False); must_change_password=Column(Boolean,default=True)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    last_seen=Column(DateTime(timezone=True),nullable=True)

    about=Column(Text,nullable=True)

    photo=Column(Text,nullable=True)

    owner_notes=Column(Text,nullable=True)

    student_id=Column(String(12),ForeignKey("students.id",ondelete="SET NULL"),nullable=True)

    student_profile=relationship("Student",foreign_keys=[student_id])

    subject_id=Column(String(12),ForeignKey("subjects.id",ondelete="SET NULL"),nullable=True)

    subject=relationship("Subject",back_populates="tutors")

    # Teamlead: ссылка для приглашённых тьюторов (NULL = независимый тьютор или teamlead)

    teamlead_id=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    no_commission=Column(Boolean,default=False)  # не взимать комиссию с этого препода

    # Финансовая модель

    subscription_model=Column(PgEnum(SubscriptionModel),nullable=True,default=None)  # percent/fixed/none

    commission_rate=Column(Integer,nullable=False,default=5,server_default='5')  # % комиссии (для percent)

    is_tutor=Column(Boolean,default=False,server_default='false')  # тимлид сам ведёт занятия

    payment_model=Column(PgEnum(PaymentModel),nullable=True,default=None)  # centralized/decentralized

    # Рекрутинг и активация

    recruited_by=Column(String(12),ForeignKey('users.id',ondelete='SET NULL'),nullable=True)

    is_active=Column(Boolean,nullable=False,default=True,server_default='true')

    # Демо-аккаунты

    is_demo=Column(Boolean,nullable=False,default=False,server_default='false')

    demo_expires_at=Column(DateTime(timezone=True),nullable=True)
    email=Column(String(200),nullable=True,unique=True)

    email_verified=Column(Boolean,nullable=False,default=False,server_default='false')

    email_verify_token=Column(String(64),nullable=True)

    email_verify_expires_at=Column(DateTime(timezone=True),nullable=True)

    trial_ends_at=Column(DateTime(timezone=True),nullable=True)

    subscription_status=Column(PgEnum(SubscriptionStatus),nullable=True,default=None)

    commission_approved_by=Column(String(12),ForeignKey('users.id',ondelete='SET NULL'),nullable=True)

    commission_approved_at=Column(DateTime(timezone=True),nullable=True)



class Subject(Base):

    __tablename__="subjects"

    id=Column(String(12),primary_key=True,default=gen_id); name=Column(String(200),unique=True,nullable=False)

    icon=Column(String(10),default="📐"); created_at=Column(DateTime(timezone=True),server_default=func.now())

    tutors=relationship("User",back_populates="subject")

    courses=relationship("Course",back_populates="subject",cascade="all, delete-orphan")



class Course(Base):

    __tablename__="courses"

    id=Column(String(12),primary_key=True,default=gen_id)

    subject_id=Column(String(12),ForeignKey("subjects.id",ondelete="CASCADE"),nullable=False)

    author_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    title=Column(String(300),nullable=False); description=Column(Text,default="")

    access=Column(PgEnum(CourseAccess),default=CourseAccess.public)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    updated_at=Column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now())

    subject=relationship("Subject",back_populates="courses")

    author=relationship("User",foreign_keys=[author_id])

    sections=relationship("CourseSection",back_populates="course",cascade="all, delete-orphan",order_by="CourseSection.position")



class CourseSection(Base):

    __tablename__="course_sections"

    id=Column(String(12),primary_key=True,default=gen_id)

    course_id=Column(String(12),ForeignKey("courses.id",ondelete="CASCADE"),nullable=False)

    title=Column(String(300),nullable=False); position=Column(Integer,nullable=False,default=0)

    idz_enabled=Column(Boolean,default=True); control_enabled=Column(Boolean,default=True)

    idz_text=Column(Text,nullable=True)

    course=relationship("Course",back_populates="sections")

    items=relationship("CourseSectionItem",back_populates="section",cascade="all, delete-orphan",order_by="CourseSectionItem.position")



class CourseSectionItem(Base):

    __tablename__="course_section_items"

    id=Column(String(12),primary_key=True,default=gen_id)

    section_id=Column(String(12),ForeignKey("course_sections.id",ondelete="CASCADE"),nullable=False)

    type=Column(String(50),nullable=False,default="topic")

    position=Column(Integer,nullable=False,default=0); name=Column(String(300),nullable=False,default="")

    total=Column(Integer,nullable=True); text=Column(Text,nullable=True); note=Column(Text,nullable=True); lang=Column(String(20),nullable=True)

    file_path=Column(String(1000),nullable=True); mime=Column(String(200),nullable=True); size=Column(Integer,nullable=True)

    section=relationship("CourseSection",back_populates="items")

    subblocks=relationship("CourseItemSubblock",back_populates="item",cascade="all,delete-orphan",order_by="CourseItemSubblock.position")



class Student(Base):

    __tablename__="students"

    id=Column(String(12),primary_key=True,default=gen_id); name=Column(String(200),nullable=False)

    level=Column(PgEnum(StudentLevelType),nullable=False,default=StudentLevelType.school)

    grade=Column(String(20),nullable=True,default="9"); goal=Column(PgEnum(GoalType),nullable=False,default=GoalType.ege)

    base_rate=Column(Integer,nullable=False,default=1500); format=Column(PgEnum(FormatType),nullable=False,default=FormatType.online)

    rewards_enabled=Column(Boolean,default=True,server_default='true')

    payer_model=Column(PgEnum(PayerModel),nullable=False,default=PayerModel.self,server_default='self')

    subject_id=Column(String(12),ForeignKey("subjects.id",ondelete="SET NULL"),nullable=True)

    created_by=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    is_demo=Column(Boolean,nullable=False,default=False,server_default='false')

    subject=relationship(Subject); creator=relationship(User,foreign_keys=[created_by])

    sections=relationship("Section",back_populates="student",cascade="all, delete-orphan",order_by="Section.position")

    courses=relationship("StudentCourse",back_populates="student",cascade="all, delete-orphan",order_by="StudentCourse.created_at")

    enrollments=relationship("Enrollment",back_populates="student",cascade="all, delete-orphan",order_by="Enrollment.created_at")

    parents=relationship("User",secondary=parent_student_link,backref="children_students")

    tutors=relationship("User",secondary=tutor_student_link,backref="assigned_students")



class StudentCourse(Base):

    __tablename__="student_courses"

    id=Column(String(12),primary_key=True,default=gen_id)

    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=False)

    tutor_id=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    title=Column(String(200),nullable=False)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    student=relationship("Student",back_populates="courses")

    tutor=relationship("User",foreign_keys=[tutor_id])

    sections=relationship("Section",back_populates="course",order_by="Section.position")



class CourseInstance(Base):

    """Программа — экземпляр курса для конкретного репетитора.

    Может быть создан из шаблона (course_id) или с нуля.

    Не привязан к ученику напрямую — связь через Enrollment."""

    __tablename__="course_instances"

    id=Column(String(12),primary_key=True,default=gen_id)

    title=Column(String(300),nullable=False,default="Программа")

    tutor_id=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    course_id=Column(String(12),ForeignKey("courses.id",ondelete="SET NULL"),nullable=True)  # шаблон-источник

    subject_id=Column(String(12),ForeignKey("subjects.id",ondelete="SET NULL"),nullable=True)

    grade=Column(String(10),nullable=True)   # "10", "11", "1 курс" и т.д.

    goal=Column(String(50),nullable=True)    # ege, oge, olymp, improve_grades, deepening, extra_education

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    updated_at=Column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now())

    is_demo=Column(Boolean,nullable=False,default=False,server_default='false')

    study_plan_id=Column(String(12),ForeignKey("study_plans.id",ondelete="SET NULL"),nullable=True)

    tutor=relationship("User",foreign_keys=[tutor_id])

    enrollments=relationship("Enrollment",back_populates="instance",cascade="all,delete-orphan")

    sections=relationship("Section",back_populates="instance",order_by="Section.position")

    study_plan=relationship("StudyPlan",back_populates="programs",foreign_keys=[study_plan_id])



class Enrollment(Base):

    """Сессия — связка ученика с экземпляром программы.

    Здесь будет жить индивидуальный прогресс (сейчас — в item напрямую)."""

    __tablename__="enrollments"

    id=Column(String(12),primary_key=True,default=gen_id)

    instance_id=Column(String(12),ForeignKey("course_instances.id",ondelete="CASCADE"),nullable=False)

    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=False)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    instance=relationship("CourseInstance",back_populates="enrollments")

    student=relationship("Student",foreign_keys=[student_id])



class Section(Base):

    __tablename__="sections"

    id=Column(String(12),primary_key=True,default=gen_id)

    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=False)

    course_id=Column(String(12),ForeignKey("student_courses.id",ondelete="SET NULL"),nullable=True)

    instance_id=Column(String(12),ForeignKey("course_instances.id",ondelete="SET NULL"),nullable=True)

    title=Column(String(300),nullable=False); position=Column(Integer,nullable=False,default=0)

    is_open=Column(Boolean,default=False); idz_enabled=Column(Boolean,default=True)

    control_enabled=Column(Boolean,default=True); idz=Column(Integer,default=0)

    control=Column(PgEnum(ControlStatus),default=ControlStatus.none)

    locked=Column(Boolean,default=False,server_default='false')

    idz_text=Column(Text,nullable=True)

    student=relationship("Student",back_populates="sections")

    course=relationship("StudentCourse",back_populates="sections")

    instance=relationship("CourseInstance",back_populates="sections")

    items=relationship("Item",back_populates="section",cascade="all, delete-orphan",order_by="Item.position")



class Item(Base):

    __tablename__="items"

    id=Column(String(12),primary_key=True,default=gen_id)

    section_id=Column(String(12),ForeignKey("sections.id",ondelete="CASCADE"),nullable=False)

    type=Column(PgEnum(ItemType),nullable=False); position=Column(Integer,nullable=False,default=0)

    name=Column(String(300)); status=Column(PgEnum(TopicStatus),default=TopicStatus.none)

    total=Column(Integer); done=Column(Integer); closed=Column(Boolean,default=False)

    date=Column(String(20)); closed_date=Column(String(20)); note=Column(Text); text=Column(Text)

    grade=Column(Integer,nullable=True); student_answer=Column(Text,nullable=True)

    lang=Column(String(20),nullable=True)

    section=relationship("Section",back_populates="items")

    attachments=relationship("Attachment",back_populates="item",cascade="all, delete-orphan")

    subblocks=relationship("ItemSubblock",back_populates="item",cascade="all,delete-orphan",order_by="ItemSubblock.position")



class Attachment(Base):

    __tablename__="attachments"

    id=Column(String(12),primary_key=True,default=gen_id)

    item_id=Column(String(12),ForeignKey("items.id",ondelete="CASCADE"),nullable=False)

    name=Column(String(500),nullable=False); mime=Column(String(200),nullable=False)

    size=Column(Integer,nullable=False); file_path=Column(String(1000))

    item=relationship("Item",back_populates="attachments")



class CourseItemSubblock(Base):

    __tablename__="course_item_subblocks"

    id=Column(String(12),primary_key=True,default=gen_id)

    item_id=Column(String(12),ForeignKey("course_section_items.id",ondelete="CASCADE"),nullable=False)

    type=Column(String(10),nullable=False,default="text")

    content=Column(Text,nullable=True); name=Column(String(300),nullable=True)

    position=Column(Integer,nullable=False,default=0)

    file_path=Column(String(1000),nullable=True); mime=Column(String(200),nullable=True); size=Column(Integer,nullable=True)

    item=relationship("CourseSectionItem",back_populates="subblocks")



class ItemSubblock(Base):

    __tablename__="item_subblocks"

    id=Column(String(12),primary_key=True,default=gen_id)

    item_id=Column(String(12),ForeignKey("items.id",ondelete="CASCADE"),nullable=False)

    type=Column(String(10),nullable=False,default="text")

    content=Column(Text,nullable=True); name=Column(String(300),nullable=True)

    position=Column(Integer,nullable=False,default=0)

    file_path=Column(String(1000),nullable=True); mime=Column(String(200),nullable=True); size=Column(Integer,nullable=True)

    item=relationship("Item",back_populates="subblocks")



class Board(Base):

    __tablename__="boards"

    id=Column(String(12),primary_key=True,default=gen_id)

    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=True)

    instance_id=Column(String(12),ForeignKey("course_instances.id",ondelete="SET NULL"),nullable=True)

    group_id=Column(String(12),ForeignKey("lesson_groups.id",ondelete="CASCADE"),nullable=True)

    strokes=Column(Text,default="[]")

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    updated_at=Column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now())

    student=relationship("Student",backref="board")

    group=relationship("Group",foreign_keys=[group_id])



class Message(Base):

    __tablename__="messages"

    id=Column(String(12),primary_key=True,default=gen_id)

    from_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    to_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    text=Column(Text,nullable=False)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    is_read=Column(Boolean,default=False,server_default='false')

    from_user=relationship("User",foreign_keys=[from_id])

    to_user=relationship("User",foreign_keys=[to_id])


class PushToken(Base):

    __tablename__="push_tokens"

    id=Column(String(12),primary_key=True,default=gen_id)

    user_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    token=Column(String(300),unique=True,nullable=False)

    platform=Column(String(20),nullable=False,default="android")

    created_at=Column(DateTime(timezone=True),server_default=func.now())



class PersonalBoard(Base):

    __tablename__="personal_boards"

    id=Column(String(12),primary_key=True,default=gen_id)

    owner_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    title=Column(String(300),nullable=False,default="Новая доска")

    strokes=Column(Text,default="[]")

    share_token=Column(String(32),nullable=True,unique=True)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    updated_at=Column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now())

    owner=relationship("User",foreign_keys=[owner_id])



student_platform_courses = Table("student_platform_courses", Base.metadata,

    Column("student_id", String(12), ForeignKey("students.id", ondelete="CASCADE"), primary_key=True),

    Column("course_id", String(12), ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True))



personal_board_share = Table("personal_board_shares", Base.metadata,

    Column("board_id", String(12), ForeignKey("personal_boards.id", ondelete="CASCADE"), primary_key=True),

    Column("user_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True))



class Notification(Base):

    __tablename__="notifications"

    id=Column(String(12),primary_key=True,default=gen_id)

    user_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    text=Column(Text,nullable=False)

    is_read=Column(Boolean,default=False,server_default='false')

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    link=Column(String(500),nullable=True)

    notif_type=Column(String(50),nullable=True)

    notif_user=relationship("User",foreign_keys=[user_id])



class ChangeRequest(Base):

    __tablename__="change_requests"

    id=Column(String(12),primary_key=True,default=gen_id)

    user_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    req_type=Column(String(20),nullable=False)

    new_value=Column(String(300),nullable=False)

    status=Column(String(20),default='pending')

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    req_user=relationship("User",foreign_keys=[user_id])



class BoardInvite(Base):

    __tablename__="board_invites"

    id=Column(String(12),primary_key=True,default=gen_id)

    board_id=Column(String(12),ForeignKey("personal_boards.id",ondelete="CASCADE"),nullable=False)

    from_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    to_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    status=Column(String(20),default='pending',nullable=False)  # pending/accepted/declined

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    board=relationship("PersonalBoard",foreign_keys=[board_id])

    from_user=relationship("User",foreign_keys=[from_id])

    to_user=relationship("User",foreign_keys=[to_id])



class ScheduleSlot(Base):

    __tablename__="schedule_slots"

    id=Column(String(12),primary_key=True,default=gen_id)

    tutor_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=True)

    day_of_week=Column(Integer,nullable=False)   # 0=Пн … 6=Вс

    slot_index=Column(Integer,nullable=False)    # 0=00:00 … 47=23:30

    duration=Column(Integer,nullable=False,default=2) # в 30-мин слотах, 2=1ч

    note=Column(String(300),nullable=True)

    student_note=Column(String(500),nullable=True)

    color=Column(String(20),nullable=True)

    is_demo=Column(Boolean,nullable=False,default=False,server_default='false')

    instance_id=Column(String(12),ForeignKey("course_instances.id",ondelete="SET NULL"),nullable=True)

    group_id=Column(String(12),ForeignKey("lesson_groups.id",ondelete="SET NULL"),nullable=True)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    tutor=relationship("User",foreign_keys=[tutor_id])

    student=relationship("Student",foreign_keys=[student_id])

    instance=relationship("CourseInstance",foreign_keys=[instance_id])

    group=relationship("Group",foreign_keys=[group_id])



chat_group_members = Table("chat_group_members", Base.metadata,

    Column("group_id", String(12), ForeignKey("chat_groups.id", ondelete="CASCADE"), primary_key=True),

    Column("user_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True))



class ChatGroup(Base):

    __tablename__="chat_groups"

    id=Column(String(12),primary_key=True,default=gen_id)

    name=Column(String(300),nullable=False)

    photo=Column(Text,nullable=True)

    created_by=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=True)

    tutor_id=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    creator=relationship("User",foreign_keys=[created_by])

    members=relationship("User",secondary="chat_group_members")



class GroupMessage(Base):

    __tablename__="group_messages"

    id=Column(String(12),primary_key=True,default=gen_id)

    group_id=Column(String(12),ForeignKey("chat_groups.id",ondelete="CASCADE"),nullable=False)

    from_id=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    text=Column(Text,nullable=False)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    sender=relationship("User",foreign_keys=[from_id])



class LessonRecord(Base):

    """Учёт проведённых занятий — для финансовой статистики."""

    __tablename__="lesson_records"

    id=Column(String(12),primary_key=True,default=gen_id)

    tutor_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=True)

    group_id=Column(String(12),ForeignKey("lesson_groups.id",ondelete="SET NULL"),nullable=True)

    slot_id=Column(String(12),ForeignKey("schedule_slots.id",ondelete="SET NULL"),nullable=True)

    held_at=Column(DateTime(timezone=True),nullable=False,server_default=func.now())

    duration_min=Column(Integer,nullable=False,default=60)

    rate=Column(Integer,nullable=False,default=1500)

    amount=Column(Integer,nullable=False,default=1500)

    note=Column(Text,nullable=True)

    created_by=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    is_auto=Column(Boolean,nullable=False,default=False,server_default='false')  # создано автоматически из расписания

    is_demo=Column(Boolean,nullable=False,default=False,server_default='false')

    commission_status=Column(String(20),nullable=False,default='not_applicable',server_default="'not_applicable'")

    commission_amount=Column(Integer,nullable=True)

    # Этап 2: статусы

    status=Column(PgEnum(LessonStatus),nullable=False,default=LessonStatus.conducted)

    payment_status=Column(PgEnum(PaymentStatus),nullable=False,default=PaymentStatus.unpaid)

    payment_confirmed_by=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    payment_confirmed_at=Column(DateTime(timezone=True),nullable=True)

    tutor=relationship("User",foreign_keys=[tutor_id])

    student_obj=relationship("Student",foreign_keys=[student_id])



class StudentPayment(Base):

    """Фиксация факта оплаты от ученика (аудит-лог)."""

    __tablename__="student_payments"

    id=Column(String(12),primary_key=True,default=gen_id)

    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=False)

    recorded_by=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    amount=Column(Integer,nullable=False)

    paid_at=Column(DateTime(timezone=True),nullable=False,server_default=func.now())

    note=Column(Text,nullable=True)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    student=relationship("Student",foreign_keys=[student_id])

    recorder=relationship("User",foreign_keys=[recorded_by])



class TeamLeadSubscription(Base):

    """Подписка teamlead — хранит период активности."""

    __tablename__="teamlead_subscriptions"

    id=Column(String(12),primary_key=True,default=gen_id)

    teamlead_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    starts_at=Column(DateTime(timezone=True),nullable=False)

    ends_at=Column(DateTime(timezone=True),nullable=False)

    plan=Column(String(50),nullable=False,default="monthly")

    price=Column(Integer,nullable=False,default=0)

    is_active=Column(Boolean,nullable=False,default=True)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    teamlead=relationship("User",foreign_keys=[teamlead_id])





class CommissionPayment(Base):

    """Факт оплаты процентной комиссии владельцу."""

    __tablename__="commission_payments"

    id=Column(String(12),primary_key=True,default=gen_id)

    user_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    amount=Column(Integer,nullable=False)

    paid_at=Column(DateTime(timezone=True),nullable=False,server_default=func.now())

    covers_lessons=Column(Text,nullable=True)  # JSON: список lesson_record.id

    status=Column(PgEnum(SubscriptionPaymentStatus),nullable=False,default=SubscriptionPaymentStatus.pending)

    recorded_by=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    note=Column(Text,nullable=True)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    user=relationship("User",foreign_keys=[user_id])

    recorder=relationship("User",foreign_keys=[recorded_by])



class TutorSubscription(Base):

    """Подписка преподавателя или тимлида (заменяет teamlead_subscriptions)."""

    __tablename__="tutor_subscriptions"

    id=Column(String(12),primary_key=True,default=gen_id)

    user_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)

    amount_monthly=Column(Integer,nullable=False,default=0)  # фикс. сумма в месяц (₽)

    started_at=Column(DateTime(timezone=True),nullable=False,server_default=func.now())

    ends_at=Column(DateTime(timezone=True),nullable=True)    # NULL = бессрочно

    is_active=Column(Boolean,nullable=False,default=True)

    note=Column(Text,nullable=True)

    created_by=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    user=relationship("User",foreign_keys=[user_id])

    creator=relationship("User",foreign_keys=[created_by])

    payments=relationship("SubscriptionPayment",back_populates="subscription",cascade="all,delete-orphan",order_by="SubscriptionPayment.paid_at.desc()")



class SubscriptionPayment(Base):

    """Факт оплаты фиксированной подписки за конкретный месяц."""

    __tablename__="subscription_payments"

    id=Column(String(12),primary_key=True,default=gen_id)

    subscription_id=Column(String(12),ForeignKey("tutor_subscriptions.id",ondelete="CASCADE"),nullable=False)

    amount=Column(Integer,nullable=False)

    period=Column(String(7),nullable=False)  # формат YYYY-MM

    paid_at=Column(DateTime(timezone=True),nullable=False,server_default=func.now())

    recorded_by=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)

    status=Column(PgEnum(SubscriptionPaymentStatus),nullable=False,default=SubscriptionPaymentStatus.pending)

    note=Column(Text,nullable=True)

    created_at=Column(DateTime(timezone=True),server_default=func.now())

    subscription=relationship("TutorSubscription",back_populates="payments")

    recorder=relationship("User",foreign_keys=[recorded_by])



class PushSub(Base):

    __tablename__ = "push_subscriptions"

    id         = Column(String(12), primary_key=True, default=gen_id)

    user_id    = Column(String(12), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    endpoint   = Column(Text, nullable=False, unique=True)

    p256dh     = Column(Text, nullable=False)

    auth       = Column(Text, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())



class Lead(Base):

    __tablename__ = "leads"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    name = Column(String, nullable=False)

    phone = Column(String, nullable=False)

    email = Column(String, nullable=True)

    subject = Column(String, nullable=True)

    source = Column(String, nullable=True)

    status = Column(String, default="new")  # new | contacted | registered | rejected

    note = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())



# ── Новые роли: recruiter, demo_tutor, demo_teamlead ─────────────────────────

# (добавляем в UserRole enum через ALTER TYPE в _startup_migrate)



class RecruitmentReward(Base):

    """Вознаграждение рекрутёра за первый платёж привлечённого пользователя."""

    __tablename__ = "recruitment_rewards"

    id = Column(String(12), primary_key=True, default=gen_id)

    recruiter_id = Column(String(12), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    recruited_user_id = Column(String(12), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    source_payment_type = Column(String(20), nullable=False)  # "subscription" | "commission"

    source_payment_id = Column(String(12), nullable=False)

    amount = Column(Integer, nullable=False)

    status = Column(String(20), nullable=False, default="pending")  # "pending" | "confirmed"

    confirmed_by = Column(String(12), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    confirmed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    recruiter = relationship("User", foreign_keys=[recruiter_id])

    recruited_user = relationship("User", foreign_keys=[recruited_user_id])

    confirmer = relationship("User", foreign_keys=[confirmed_by])



class BoardAnomalyFlag(Base):

    """Флаг аномальной сессии доски без слота в расписании."""

    __tablename__ = "board_anomaly_flags"

    id = Column(String(12), primary_key=True, default=gen_id)

    tutor_id = Column(String(12), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    student_id = Column(String(12), ForeignKey("students.id", ondelete="CASCADE"), nullable=False)

    session_start = Column(DateTime(timezone=True), nullable=False)

    session_duration_min = Column(Integer, nullable=False, default=0)

    status = Column(String(20), nullable=False, default="open")  # "open"|"dismissed"|"resolved"

    dismissed_by = Column(String(12), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    dismissed_at = Column(DateTime(timezone=True), nullable=True)

    note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tutor = relationship("User", foreign_keys=[tutor_id])

    dismisser = relationship("User", foreign_keys=[dismissed_by])





# ── Групповые занятия ─────────────────────────────────────────────────────────



class Group(Base):

    """Группа учеников для совместных занятий."""

    __tablename__ = "lesson_groups"

    id           = Column(String(12), primary_key=True, default=gen_id)

    name         = Column(String(255), nullable=False)

    tutor_id     = Column(String(12), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    subject_id   = Column(String(12), ForeignKey("subjects.id", ondelete="SET NULL"), nullable=True)

    color        = Column(String(20), nullable=True)

    note         = Column(Text, nullable=True)

    max_students = Column(Integer, nullable=True)

    is_demo      = Column(Boolean, nullable=False, default=False, server_default="false")

    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    chat_group_id = Column(String(12), ForeignKey("chat_groups.id", ondelete="SET NULL"), nullable=True)

    tutor         = relationship("User", foreign_keys=[tutor_id])

    chat_group    = relationship("ChatGroup", foreign_keys=[chat_group_id])

    memberships   = relationship("GroupMembership", back_populates="group", cascade="all,delete-orphan")

    course_links  = relationship("GroupCourseLink", back_populates="group", cascade="all,delete-orphan")



class GroupMembership(Base):

    """Участник группы. Soft-delete через left_at."""

    __tablename__ = "group_memberships"

    id         = Column(String(12), primary_key=True, default=gen_id)

    group_id   = Column(String(12), ForeignKey("lesson_groups.id", ondelete="CASCADE"), nullable=False)

    student_id = Column(String(12), ForeignKey("students.id", ondelete="CASCADE"), nullable=False)

    joined_at  = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    left_at    = Column(DateTime(timezone=True), nullable=True)

    group      = relationship("Group", back_populates="memberships")

    student    = relationship("Student", foreign_keys=[student_id])



class GroupCourseLink(Base):

    """Привязка группы к программе (CourseInstance). Хранит ставку."""

    __tablename__ = "group_course_links"

    id          = Column(String(12), primary_key=True, default=gen_id)

    group_id    = Column(String(12), ForeignKey("lesson_groups.id", ondelete="CASCADE"), nullable=False)

    instance_id = Column(String(12), ForeignKey("course_instances.id", ondelete="CASCADE"), nullable=False)

    rate        = Column(Integer, nullable=False, default=0)

    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    group       = relationship("Group", back_populates="course_links")

    instance    = relationship("CourseInstance", foreign_keys=[instance_id])



# ── Планы занятий ─────────────────────────────────────────────────────────────



class StudyPlan(Base):

    """План занятий — договорённость: кто учится, с кем, по какому предмету, с какой целью.

    У одного ученика/группы может быть несколько планов (разные предметы, разные цели)."""

    __tablename__ = "study_plans"

    id         = Column(String(12), primary_key=True, default=gen_id)

    tutor_id   = Column(String(12), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    student_id = Column(String(12), ForeignKey("students.id", ondelete="CASCADE"), nullable=True)

    group_id   = Column(String(12), ForeignKey("lesson_groups.id", ondelete="CASCADE"), nullable=True)

    subject_id = Column(String(12), ForeignKey("subjects.id", ondelete="SET NULL"), nullable=True)

    goal       = Column(Text, nullable=True)   # цель в свободном тексте

    status     = Column(PgEnum(StudyPlanStatus), nullable=False, default=StudyPlanStatus.active)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tutor      = relationship("User", foreign_keys=[tutor_id])

    student    = relationship("Student", foreign_keys=[student_id])

    group      = relationship("Group", foreign_keys=[group_id])

    subject    = relationship("Subject", foreign_keys=[subject_id])

    programs   = relationship("CourseInstance", back_populates="study_plan")



