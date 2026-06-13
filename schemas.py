from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class LoginRequest(BaseModel):
    login: str; password: str
class LoginResponse(BaseModel):
    token: str; role: str; name: str; must_change_password: bool = False
class ChangePasswordRequest(BaseModel):
    old_password: str; new_password: str

class GoalCase(BaseModel):
    subject_id: Optional[str] = None
    goal: str = "ege"

class StudentRegister(BaseModel):
    login: str; password: str; name: str
    grade: str = "9"
    goal_cases: list[GoalCase] = []
    # legacy (для обратной совместимости)
    goal: str = "ege"
    subject_id: Optional[str] = None
    subject_ids: list[str] = []

class ProRegister(BaseModel):
    login: str; password: str; name: str; email: str
    role: str  # tutor | teamlead | recruiter | parent
    subscription_type: Optional[str] = None  # percent | fixed

class UserCreate(BaseModel):
    login: str; password: str; role: str; name: str
    student_id: Optional[str]=None; children_ids: list[str]=[]; subject_id: Optional[str]=None
    subject_ids: list[str]=[]
class UserOut(BaseModel):
    id: str; login: str; role: str; name: str; must_change_password: bool = False
    student_id: Optional[str]=None; subject_id: Optional[str]=None; created_at: Optional[datetime]=None
    last_seen: Optional[datetime]=None; subject_ids: list[str]=[]
    about: Optional[str]=None; photo: Optional[str]=None
    teamlead_id: Optional[str]=None
    no_commission: bool = False
    subscription_model: Optional[str]=None
    commission_rate: int = 5
    is_tutor: bool = False
    payment_model: Optional[str]=None
    recruited_by: Optional[str]=None
    is_active: bool = True
    is_demo: bool = False
    demo_expires_at: Optional[datetime]=None
    email: Optional[str]=None
    email_verified: bool=False
    trial_ends_at: Optional[datetime]=None
    subscription_status: Optional[str]=None
    is_recruiting: bool = False
    model_config = {"from_attributes": True}

class ProfileUpdate(BaseModel):
    name: Optional[str]=None
    about: Optional[str]=None
    photo: Optional[str]=None

class OwnerProfileUpdate(BaseModel):
    name: Optional[str]=None
    about: Optional[str]=None
    photo: Optional[str]=None
    owner_notes: Optional[str]=None
    login: Optional[str]=None
    password: Optional[str]=None
    no_commission: Optional[bool]=None
    subscription_model: Optional[str]=None
    commission_rate: Optional[int]=None
    is_tutor: Optional[bool]=None
    payment_model: Optional[str]=None

class ChangeRequestCreate(BaseModel):
    req_type: str
    new_value: str

class NotificationOut(BaseModel):
    id: str; text: str; is_read: bool
    created_at: Optional[datetime]=None
    link: Optional[str]=None; notif_type: Optional[str]=None
    model_config = {"from_attributes": True}

class ContactOut(BaseModel):
    id: str; name: str; role: str; last_seen: Optional[datetime]=None
    model_config = {"from_attributes": True}

class SubjectCreate(BaseModel):
    name: str; icon: str = "📐"
class SubjectUpdate(BaseModel):
    name: Optional[str]=None; icon: Optional[str]=None
class SubjectOut(BaseModel):
    id: str; name: str; icon: str; created_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

class SubblockCreate(BaseModel):
    content: Optional[str]=None

class CourseItemSubblockOut(BaseModel):
    id: str; item_id: str; type: str; content: Optional[str]=None
    name: Optional[str]=None; position: int
    file_path: Optional[str]=None; mime: Optional[str]=None; size: Optional[int]=None
    model_config = {"from_attributes": True}

class ItemSubblockOut(BaseModel):
    id: str; item_id: str; type: str; content: Optional[str]=None
    name: Optional[str]=None; position: int
    file_path: Optional[str]=None; mime: Optional[str]=None; size: Optional[int]=None
    model_config = {"from_attributes": True}

class CourseSectionItemCreate(BaseModel):
    name: str=""; type: str="topic"
    total: Optional[int]=None; text: Optional[str]=None
class CourseSectionItemUpdate(BaseModel):
    name: Optional[str]=None; type: Optional[str]=None
    total: Optional[int]=None; text: Optional[str]=None; note: Optional[str]=None; lang: Optional[str]=None
class CourseSectionItemOut(BaseModel):
    id: str; section_id: str; type: str; position: int; name: str
    total: Optional[int]=None; text: Optional[str]=None; note: Optional[str]=None; lang: Optional[str]=None
    file_path: Optional[str]=None; mime: Optional[str]=None; size: Optional[int]=None
    subblocks: list[CourseItemSubblockOut] = []
    model_config = {"from_attributes": True}
class CourseSectionCreate(BaseModel):
    title: str; idz_enabled: bool=True; control_enabled: bool=True
    idz_text: Optional[str]=None
    items: list[CourseSectionItemCreate] = []
class CourseSectionUpdate(BaseModel):
    title: Optional[str]=None; idz_enabled: Optional[bool]=None; control_enabled: Optional[bool]=None
    idz_text: Optional[str]=None
class CourseSectionOut(BaseModel):
    id: str; course_id: str; title: str; position: int; idz_enabled: bool; control_enabled: bool
    idz_text: Optional[str]=None
    items: list[CourseSectionItemOut] = []
    model_config = {"from_attributes": True}
class CourseCreate(BaseModel):
    subject_id: str; title: str; description: str=""; access: str="public"
class CourseUpdate(BaseModel):
    title: Optional[str]=None; description: Optional[str]=None; access: Optional[str]=None
class CourseOut(BaseModel):
    id: str; subject_id: str; author_id: str; title: str; description: str; access: str
    created_at: Optional[datetime]=None; updated_at: Optional[datetime]=None
    sections: list[CourseSectionOut] = []
    model_config = {"from_attributes": True}
class CourseListItem(BaseModel):
    id: str; subject_id: str; author_id: str; title: str; description: str; access: str
    author_name: Optional[str]=None; subject_name: Optional[str]=None
    sections_count: int=0; created_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

class SaveAsCourseRequest(BaseModel):
    subject_id: str; title: str; access: str="public"; replace_id: Optional[str]=None

class AttachmentOut(BaseModel):
    id: str; item_id: str; name: str; mime: str; size: int; file_path: Optional[str]=None
    model_config = {"from_attributes": True}

class ItemCreate(BaseModel):
    type: str; name: Optional[str]=None; status: Optional[str]="none"
    total: Optional[int]=None; done: Optional[int]=None; closed: Optional[bool]=False
    date: Optional[str]=None; closed_date: Optional[str]=None; note: Optional[str]=None; text: Optional[str]=None
    grade: Optional[int]=None
class ItemUpdate(BaseModel):
    name: Optional[str]=None; status: Optional[str]=None; total: Optional[int]=None
    done: Optional[int]=None; closed: Optional[bool]=None; closed_date: Optional[str]=None
    note: Optional[str]=None; text: Optional[str]=None; grade: Optional[int]=None
    student_answer: Optional[str]=None; lang: Optional[str]=None
class ItemOut(BaseModel):
    id: str; section_id: str; type: str; position: int
    name: Optional[str]=None; status: Optional[str]=None; total: Optional[int]=None
    done: Optional[int]=None; closed: Optional[bool]=None; date: Optional[str]=None
    closed_date: Optional[str]=None; note: Optional[str]=None; text: Optional[str]=None
    grade: Optional[int]=None; student_answer: Optional[str]=None; lang: Optional[str]=None
    attachments: list[AttachmentOut] = []
    subblocks: list[ItemSubblockOut] = []
    model_config = {"from_attributes": True}

class SectionCreate(BaseModel):
    title: str; idz_enabled: bool=True; control_enabled: bool=True; idz_text: Optional[str]=None
    course_id: Optional[str]=None
class SectionUpdate(BaseModel):
    title: Optional[str]=None; is_open: Optional[bool]=None; idz_enabled: Optional[bool]=None
    control_enabled: Optional[bool]=None; idz: Optional[int]=None; control: Optional[str]=None
    locked: Optional[bool]=None; idz_text: Optional[str]=None; course_id: Optional[str]=None
class SectionOut(BaseModel):
    id: str; student_id: str; title: str; position: int; is_open: bool
    idz_enabled: bool; control_enabled: bool; idz: int; control: str
    locked: bool=False; idz_text: Optional[str]=None; course_id: Optional[str]=None
    items: list[ItemOut] = []
    model_config = {"from_attributes": True}

class StudentCourseCreate(BaseModel):
    title: str; tutor_id: Optional[str]=None
class StudentCourseUpdate(BaseModel):
    title: Optional[str]=None; tutor_id: Optional[str]=None
class StudentCourseOut(BaseModel):
    id: str; student_id: str; title: str
    tutor_id: Optional[str]=None; tutor_name: Optional[str]=None
    sections: list[SectionOut] = []
    model_config = {"from_attributes": True}

# ── Программы (экземпляры курсов) ────────────────────────────────────────────

class CourseInstanceCreate(BaseModel):
    title: str
    tutor_id: Optional[str]=None
    course_id: Optional[str]=None
    subject_id: Optional[str]=None
    grade: Optional[str]=None
    goal: Optional[str]=None

class CourseInstanceUpdate(BaseModel):
    title: Optional[str]=None
    tutor_id: Optional[str]=None
    subject_id: Optional[str]=None
    grade: Optional[str]=None
    goal: Optional[str]=None

class CourseInstanceOut(BaseModel):
    id: str; title: str
    tutor_id: Optional[str]=None; tutor_name: Optional[str]=None
    course_id: Optional[str]=None
    subject_id: Optional[str]=None; subject_name: Optional[str]=None
    grade: Optional[str]=None; goal: Optional[str]=None
    created_at: Optional[datetime]=None
    sections: list[SectionOut] = []
    model_config = {"from_attributes": True}

class CourseInstanceListItem(BaseModel):
    id: str; title: str
    tutor_id: Optional[str]=None; tutor_name: Optional[str]=None
    subject_id: Optional[str]=None; subject_name: Optional[str]=None
    grade: Optional[str]=None; goal: Optional[str]=None
    sections_count: int=0
    created_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

class EnrollmentCreate(BaseModel):
    instance_id: str

class EnrollmentOut(BaseModel):
    id: str; instance_id: str; student_id: str
    created_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

class StudentCreate(BaseModel):
    name: str; level: str="school"; grade: Optional[str]=None; goal: str="ege"
    base_rate: int=1500; format: str="online"
    subject_id: Optional[str]=None
class StudentUpdate(BaseModel):
    name: Optional[str]=None; level: Optional[str]=None; grade: Optional[str]=None
    goal: Optional[str]=None; base_rate: Optional[int]=None; format: Optional[str]=None
    subject_id: Optional[str]=None; rewards_enabled: Optional[bool]=None
    payer_model: Optional[str]=None
class StudentOut(BaseModel):
    id: str; name: str; level: str="school"; grade: Optional[str]=None; goal: str; base_rate: int; format: str
    rewards_enabled: bool=True
    payer_model: str="self"
    subject_id: Optional[str]=None; created_by: Optional[str]=None; created_at: Optional[datetime]=None
    is_searching: bool=False
    sections: list[SectionOut] = []
    courses: list[StudentCourseOut] = []
    model_config = {"from_attributes": True}
class StudentListItem(BaseModel):
    id: str; name: str; level: str="school"; grade: Optional[str]=None; goal: str; base_rate: int; format: str
    rewards_enabled: bool=True
    payer_model: str="self"
    subject_id: Optional[str]=None; created_by: Optional[str]=None; created_at: Optional[datetime]=None
    is_searching: bool=False
    model_config = {"from_attributes": True}

class StudentPaymentCreate(BaseModel):
    student_id: str; amount: int; paid_at: Optional[datetime]=None; note: Optional[str]=None

class StudentPaymentOut(BaseModel):
    id: str; student_id: str; recorded_by: Optional[str]=None; amount: int
    paid_at: Optional[datetime]=None; note: Optional[str]=None; created_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

class BoardOut(BaseModel):
    id: str; student_id: str; strokes: str
    created_at: Optional[datetime]=None; updated_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

from datetime import datetime as dt_type
class MessageCreate(BaseModel):
    to_id: str; text: str
class PushTokenIn(BaseModel):
    token: str; platform: str="android"
class MessageOut(BaseModel):
    id: str; from_id: str; to_id: str; text: str; is_read: bool
    created_at: Optional[dt_type]=None; from_name: str=""; to_name: str=""
    model_config = {"from_attributes": True}

class ScheduleSlotCreate(BaseModel):
    student_id: Optional[str]=None
    instance_id: Optional[str]=None
    group_id: Optional[str]=None
    day_of_week: int
    slot_index: int
    duration: int=2
    note: Optional[str]=None
    color: Optional[str]=None

class PersonalBoardCreate(BaseModel):
    title: str="Новая доска"
class PersonalBoardUpdate(BaseModel):
    title: Optional[str]=None
class PersonalBoardOut(BaseModel):
    id: str; owner_id: str; title: str; strokes: str
    share_token: Optional[str]=None
    created_at: Optional[dt_type]=None; updated_at: Optional[dt_type]=None
    model_config = {"from_attributes": True}
class PersonalBoardListItem(BaseModel):
    id: str; owner_id: str; title: str; share_token: Optional[str]=None
    created_at: Optional[dt_type]=None; updated_at: Optional[dt_type]=None
    is_owner: bool = True
    owner_name: Optional[str] = None
    member_count: int = 0
    model_config = {"from_attributes": True}

class BoardMemberOut(BaseModel):
    id: str; name: str; role: str
    model_config = {"from_attributes": True}

class BoardInviteOut(BaseModel):
    id: str; board_id: str; board_title: str; from_id: str; from_name: str
    created_at: Optional[dt_type] = None
    model_config = {"from_attributes": True}

# ── Подписки ─────────────────────────────────────────────────────────────────

class TutorSubscriptionCreate(BaseModel):
    user_id: str
    amount_monthly: int
    started_at: Optional[dt_type] = None
    ends_at: Optional[dt_type] = None
    is_active: bool = True
    note: Optional[str] = None

class TutorSubscriptionUpdate(BaseModel):
    amount_monthly: Optional[int] = None
    started_at: Optional[dt_type] = None
    ends_at: Optional[dt_type] = None
    is_active: Optional[bool] = None
    note: Optional[str] = None

class SubscriptionPaymentOut(BaseModel):
    id: str; subscription_id: str; amount: int; period: str
    paid_at: Optional[dt_type] = None
    recorded_by: Optional[str] = None
    status: str; note: Optional[str] = None
    created_at: Optional[dt_type] = None
    model_config = {"from_attributes": True}

class TutorSubscriptionOut(BaseModel):
    id: str; user_id: str; amount_monthly: int
    started_at: Optional[dt_type] = None
    ends_at: Optional[dt_type] = None
    is_active: bool; note: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[dt_type] = None
    user_name: Optional[str] = None
    user_role: Optional[str] = None
    payments: list[SubscriptionPaymentOut] = []
    model_config = {"from_attributes": True}

class SubscriptionPaymentCreate(BaseModel):
    amount: int
    period: str  # YYYY-MM
    paid_at: Optional[dt_type] = None
    note: Optional[str] = None

class CommissionPaymentCreate(BaseModel):
    amount: int
    paid_at: Optional[dt_type] = None
    covers_lessons: Optional[list[str]] = None  # список lesson_record.id
    note: Optional[str] = None

class CommissionPaymentOut(BaseModel):
    id: str; user_id: str; amount: int
    paid_at: Optional[dt_type] = None
    covers_lessons: Optional[str] = None  # JSON string
    status: str; recorded_by: Optional[str] = None
    note: Optional[str] = None; created_at: Optional[dt_type] = None
    model_config = {"from_attributes": True}

# ── Рекрутинг ─────────────────────────────────────────────────────────────────

class RecruitmentRewardOut(BaseModel):
    id: str
    recruiter_id: str
    recruited_user_id: str
    recruited_user_name: Optional[str] = None
    source_payment_type: str
    source_payment_id: str
    amount: int
    status: str
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    model_config = {"from_attributes": True}

class RecruitedUserOut(BaseModel):
    id: str
    login: str
    name: str
    role: str
    is_active: bool = True
    recruited_by: Optional[str] = None
    created_at: Optional[datetime] = None
    first_payment_status: Optional[str] = None  # None | "pending" | "confirmed"
    first_payment_amount: Optional[int] = None
    reward_status: Optional[str] = None  # None | "pending" | "confirmed"
    model_config = {"from_attributes": True}

class DemoAccountCreate(BaseModel):
    role: str  # "demo_tutor" | "demo_teamlead"
    name: str
    mode: str = "empty"  # "empty" | "prefilled"

class DemoAccountOut(BaseModel):
    id: str
    login: str
    password: str
    name: str
    role: str
    expires_at: Optional[datetime] = None
    is_expired: bool = False
    created_by: Optional[str] = None

# ── Антифрод ──────────────────────────────────────────────────────────────────

class BoardAnomalyFlagOut(BaseModel):
    id: str
    tutor_id: str
    tutor_name: Optional[str] = None
    student_id: str
    student_name: Optional[str] = None
    session_start: Optional[datetime] = None
    session_duration_min: int = 0
    status: str
    dismissed_by: Optional[str] = None
    dismissed_at: Optional[datetime] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    model_config = {"from_attributes": True}

# ── Атрибуция владения ────────────────────────────────────────────────────────

class TutorRef(BaseModel):
    id: str
    name: str

class StudentListItemWithAttribution(BaseModel):
    id: str; name: str; level: str = "school"; grade: Optional[str] = None
    goal: str; base_rate: int; format: str
    rewards_enabled: bool = True
    payer_model: str = "self"
    subject_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    is_demo: bool = False
    tutor: Optional[TutorRef] = None
    teamlead: Optional[TutorRef] = None
    model_config = {"from_attributes": True}
