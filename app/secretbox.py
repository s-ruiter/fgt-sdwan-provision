"""Encrypted storage for passwords the tool has to use again later (the Proxmox console password).

Fernet (AES-128-CBC + HMAC-SHA256). The key lives in its own file `console.key` (0600) in the tool's data folder,
created on first use. A leaked database or export holds no usable password; root on the tool host can still decrypt
(key and database are on the same machine)."""
import os

from cryptography.fernet import Fernet, InvalidToken

KEY_FILE = "console.key"


def _key(data_dir, create):
    path = os.path.join(data_dir, KEY_FILE)
    if not os.path.exists(path):
        if not create:
            raise ValueError("no console key: the password cannot be decrypted (store it again)")
        os.makedirs(data_dir, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        except FileExistsError:                              # created at the same moment by another request
            pass
    with open(path, "rb") as f:
        return f.read().strip()


def seal(data_dir, text):
    return Fernet(_key(data_dir, True)).encrypt(text.encode()).decode()


def unseal(data_dir, token):
    try:
        return Fernet(_key(data_dir, False)).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as e:
        raise ValueError(f"the stored console password cannot be decrypted ({type(e).__name__}): store it again") from None
