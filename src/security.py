import hashlib
import secrets

from argon2.exceptions import VerificationError
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError


password_hasher = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = password_hasher.hash("chexu-dummy-password-not-an-account")


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("密码长度必须为 12～128 个字符。")
    return password_hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return password_hasher.verify(password, stored_hash)
    except (PwdlibError, ValueError, VerificationError):
        return False


def create_access_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    # 随机令牌只保存摘要；员工密码使用上面的 Argon2，二者不能混用。
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
