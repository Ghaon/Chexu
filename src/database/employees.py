import argparse
from getpass import getpass
import re
import sys

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.database.connection import SessionFactory, engine
from src.database.models import EmployeeRecord
from src.security import hash_password


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Chexu employee account.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", choices=["sales", "manager"], required=True)
    args = parser.parse_args()
    username = args.username.strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_.-]{2,31}", username) is None:
        print("用户名须为 3～32 位英文字母、数字、下划线、点或短横线，并以字母开头。")
        return 1
    if not sys.stdin.isatty():
        print("请在交互式终端中运行，密码不会通过命令行参数传入。")
        return 1
    try:
        password = getpass("Password (12-128 characters): ")
        repeated = getpass("Repeat password: ")
        if password != repeated:
            print("两次密码不一致，未创建账号。")
            return 1
        stored_hash = hash_password(password)
        with SessionFactory.begin() as session:
            employee = EmployeeRecord(username=username, role=args.role, password_hash=stored_hash)
            session.add(employee)
            session.flush()
            employee_id = employee.employee_id
        print(f"Employee created: {username} ({args.role}), id={employee_id}")
        return 0
    except ValueError as exc:
        print(str(exc))
        return 1
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        print("用户名已存在。" if constraint == "uq_employees_username" else "员工账号保存失败。")
        return 1
    except SQLAlchemyError:
        print("数据库暂时不可用，未创建账号。")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("账号创建已取消。")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
