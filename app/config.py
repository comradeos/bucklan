import os
from pathlib import Path
from typing import Optional

from . import messages


ENV_FILENAME = ".env"
ENV_ENCODING = "utf-8-sig"
ENV_SEPARATOR = "="
ENV_QUOTES = ('"', "'")
ROOT_VARIABLE = "BUCKLAN_ROOT"
PORT_VARIABLE = "BUCKLAN_PORT"
TOKEN_VARIABLE = "BUCKLAN_TOKEN"
TOKEN_SYMBOLS = "-_"
DEFAULT_PORT = 8765


# Читає налаштування зі змінної середовища або з файла .env.
def read_setting(key: str, env_file: Path) -> Optional[str]:
    if key in os.environ:
        return os.environ[key]

    try:
        lines = env_file.read_text(encoding=ENV_ENCODING).splitlines()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise ValueError(messages.ENV_READ_ERROR.format(env_file=env_file, error=error)) from error

    for line in lines:
        name, separator, value = line.partition(ENV_SEPARATOR)

        if name.strip() != key:
            continue
        
        if not separator:
            raise ValueError(messages.ENV_INVALID_ENTRY.format(key=key, env_file=env_file))

        value = value.strip()
        
        if value.startswith(ENV_QUOTES):
            if len(value) < 2 or value[0] != value[-1]:
                raise ValueError(messages.ENV_UNCLOSED_QUOTE.format(key=key, env_file=env_file))
            
            value = value[1:-1]
        
        return value

    return None


# Читає порт і перевіряє, чи належить він до діапазону 1–65535.
def read_port(env_file: Path) -> int:
    value = read_setting(PORT_VARIABLE, env_file)
    
    if value is None:
        return DEFAULT_PORT
    
    if value.isascii() and value.isdecimal() and len(value) <= 5:
        port = int(value)
    
        if 1 <= port <= 65535:
            return port
    
    raise ValueError(messages.INVALID_PORT.format(key=PORT_VARIABLE))


# Перевіряє довжину й допустимі символи токена доступу.
def validate_token(token: Optional[str]) -> str:
    if token and len(token) >= 32 and token.isascii():
        if all(character.isalnum() or character in TOKEN_SYMBOLS for character in token):
            return token

    raise ValueError(messages.INVALID_TOKEN.format(key=TOKEN_VARIABLE))
