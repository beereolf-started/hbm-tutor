from database import engine, Base, SessionLocal
from models import (User, Student, Section, Item, Attachment, Subject,
    Course, CourseSection, CourseSectionItem, Board,
    parent_student_link, tutor_student_link)
from auth import hash_password

def init():
    print("Создаю таблицы...")
    Base.metadata.create_all(bind=engine)
    for t in Base.metadata.sorted_tables: print(f"  ✓ {t.name}")
    db = SessionLocal()
    try:
        math = db.query(Subject).filter(Subject.name=="Математика").first()
        if not math:
            math = Subject(name="Математика", icon="📐")
            db.add(math); db.flush()
            print("\n  Создан предмет: 📐 Математика")
        owner = db.query(User).filter(User.role=="owner").first()
        if not owner:
            owner = User(login="admin", password_hash=hash_password("admin123"),
                role="owner", name="Владелец", must_change_password=True, subject_id=math.id)
            db.add(owner); db.commit()
            print("  Аккаунт владельца: admin / admin123")
        else:
            print(f"  Владелец уже есть: {owner.login}")
            db.commit()
    finally: db.close()
    print("Готово!")

if __name__ == "__main__": init()
