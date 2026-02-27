# ╔══════════════════════════════════════════════════════════════╗
# ║  HBM РЕПЕТИТОР — init_db.py v1.1                            ║
# ║  Создание таблиц + аккаунт репетитора                       ║
# ║  Запуск: python init_db.py                                   ║
# ╚══════════════════════════════════════════════════════════════╝

from database import engine, Base, SessionLocal
from models import User, Student, Section, Item, Attachment, parent_student_link
from auth import hash_password

def init():
    print("Создаю таблицы в БД hbm...")
    Base.metadata.create_all(bind=engine)
    for table in Base.metadata.sorted_tables:
        print(f"  ✓ {table.name}")

    # Создать аккаунт репетитора, если его ещё нет
    db = SessionLocal()
    try:
        tutor = db.query(User).filter(User.role == "tutor").first()
        if not tutor:
            tutor = User(
                login="admin",
                password_hash=hash_password("admin123"),
                role="tutor",
                name="Репетитор",
                must_change_password=True,
            )
            db.add(tutor)
            db.commit()
            print("\n  Создан аккаунт репетитора:")
            print("    Логин:  admin")
            print("    Пароль: admin123")
            print("    (смени пароль при первом входе!)")
        else:
            print(f"\n  Аккаунт репетитора уже есть: {tutor.login}")
    finally:
        db.close()

    print("\nГотово!")

if __name__ == "__main__":
    init()
